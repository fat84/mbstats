from collections import defaultdict
from influxdb import InfluxDBClient
from pygtail import Pygtail
import argparse
import cPickle as pickle
import csv
import datetime
import gzip
import itertools
import json
import math
import os.path
import platform
import re
import shutil
import sys



# https://github.com/metabrainz/openresty-gateways/blob/master/files/nginx/nginx.conf#L23
fieldnames = [
    'version',
    'msec',
    'vhost',
    'protocol',
    'loctag',
    'status',
    'bytes_sent',
    'gzip_ratio',
    'request_length',
    'request_time',
    'upstream_addr',
    'upstream_status',
    'upstream_response_time',
    'upstream_connect_time',
    'upstream_header_time',
]

mbs_tags = {
    'hits': ('vhost', 'protocol', 'loctag'),
    'status': ('vhost', 'protocol', 'loctag', 'status'),
    'bytes_sent': ('vhost', 'protocol', 'loctag'),
    'gzip_count': ('vhost', 'protocol', 'loctag'),
    'gzip_percent': ('vhost', 'protocol', 'loctag'),
    'gzip_ratio_mean': ('vhost', 'protocol', 'loctag'),
    'request_length_mean': ('vhost', 'protocol', 'loctag'),
    'request_time_mean': ('vhost', 'protocol', 'loctag'),
    'upstreams_hits': ('vhost', 'protocol', 'loctag', 'upstream'),
    'upstreams_status': ('vhost', 'protocol', 'loctag', 'upstream', 'status'),
    'upstreams_servers_contacted': ('vhost', 'protocol', 'loctag'),
    'upstreams_internal_redirects': ('vhost', 'protocol', 'loctag'),
    'upstreams_servers': ('vhost', 'protocol', 'loctag'),
    'upstreams_response_time_mean': ('vhost', 'protocol', 'loctag', 'upstream'),
    'upstreams_connect_time_mean': ('vhost', 'protocol', 'loctag', 'upstream'),
    'upstreams_header_time_mean': ('vhost', 'protocol', 'loctag', 'upstream'),
}

def dd_int():
    return defaultdict(int)

def dd_float():
    return defaultdict(float)

def dd_any():
    return defaultdict()

def factory():
    return lambda x: x
types = defaultdict(factory)
types['upstream_status'] = lambda x: int(x)
types['upstream_response_time'] = types['upstream_connect_time'] = types['upstream_header_time'] = lambda x: float(x)

def split_upstream(s):
    return [x.split(' : ') for x in s.split(", ")]

def parse_upstreams(row):
    #servers were contacted ", "
    #internal redirect " : "
    r = dict()
    chains = {}

    splitted = split_upstream(row['upstream_addr'])
    r['servers_contacted'] = len(splitted)
    r['internal_redirects'] = len([x for x in splitted if len(x) > 1])
    chains['upstream_addr'] = list(itertools.chain.from_iterable(splitted))

    for k in (  'upstream_status',
                'upstream_response_time',
                'upstream_connect_time',
                'upstream_header_time'
             ):
        chains[k] = list(itertools.chain.from_iterable(split_upstream(row[k])))
    r['status'] = defaultdict(dd_int)
    r['response_time'] = defaultdict(float)
    r['connect_time'] = defaultdict(float)
    r['header_time'] = defaultdict(float)
    r['servers'] = []
    for item in zip(chains['upstream_addr'],
                    chains['upstream_status'],
                    chains['upstream_response_time'],
                    chains['upstream_connect_time'],
                    chains['upstream_header_time']
                   ):
        k = item[0]
        r['servers'].append(k)
        r['status'][k][item[1]] += 1
        r['response_time'][k] += float(item[2])
        r['connect_time'][k] += float(item[3])
        r['header_time'][k] += float(item[4])
    return r
    #print(r)
    #print("\n")

def parsefile(pygtail, maxlines = 1000, start=0):
    storage = defaultdict(list)
    n = 0
    for line in pygtail:
        n += 1
        if n < start:
            continue
        try:
            items = line.rstrip('\r\n').split('|')
            for index, item in enumerate(items):
                if item == '-':
                    items[index] = None
            row = dict(zip(fieldnames, items))
 #           row['datetime'] = datetime.datetime.utcfromtimestamp(
 #               float(row['msec'])
 #           )
            for k in ['status', 'bytes_sent', 'request_length']:
                if row[k] is not None:
                    row[k] = int(row[k])
            for k in ['gzip_ratio', 'request_time']:
                if row[k] is not None:
                    row[k] = float(row[k])
            if row['upstream_addr'] is not None:
                row['upstreams'] = parse_upstreams(row)
            else:
                row['upstreams'] = None
            #row = {k: v for k, v in row.items() if v != '-'}
#            minute = int(row['datetime'].strftime('%Y%m%d%H%M'))
#            YYYYMMDDHHMM
#            1000 100000000
#            d = row['datetime']
            d = datetime.datetime.utcfromtimestamp(
                float(row['msec'])
            )
            minute = d.minute + d.hour * 100 + d.day * 100**2 + d.month * 100**3 + d.year * 100**4

            del row['msec']
            del row['version']
            del row['upstream_addr']
            del row['upstream_status']
            del row['upstream_response_time']
            del row['upstream_connect_time']
            del row['upstream_header_time']

            storage[minute].append(row)
        except ValueError as e:
            print(e, line)
        maxlines -= 1
        if maxlines == 0:
            break
    pygtail._update_offset_file()
    return storage


def parse_storage(storage, previous_leftover = None):
    leftover = dict()
    mbs = dict()
    mbs['hits'] = defaultdict(int)
    mbs['status'] = defaultdict(int)
    mbs['bytes_sent'] = defaultdict(int)
    mbs['gzip_count'] = defaultdict(int)
    mbs['_gzip_ratio_premean'] = defaultdict(float)
    mbs['_request_length_premean'] = defaultdict(int)
    mbs['_request_time_premean'] = defaultdict(float)
    mbs['upstreams_hits'] = defaultdict(int)
    mbs['upstreams_status'] = defaultdict(int)
    mbs['upstreams_servers_contacted'] = defaultdict(int)
    mbs['upstreams_internal_redirects'] = defaultdict(int)
    mbs['upstreams_servers'] = defaultdict(int)
    mbs['_upstreams_response_time_premean'] = defaultdict(float)
    mbs['_upstreams_connect_time_premean'] = defaultdict(float)
    mbs['_upstreams_header_time_premean'] = defaultdict(float)

    mbs['gzip_percent'] = defaultdict(float)
    mbs['gzip_ratio_mean'] = defaultdict(float)
    mbs['request_length_mean'] = defaultdict(float)
    mbs['request_time_mean'] = defaultdict(float)
    mbs['upstreams_response_time_mean'] = defaultdict(float)
    mbs['upstreams_connect_time_mean'] = defaultdict(float)
    mbs['upstreams_header_time_mean'] = defaultdict(float)

    last_minute = max(storage.keys())

    def statmins(sto, name):
        first = int(min(sto.keys()))
        last = int(max(sto.keys()))
        length = len(sto.keys())
        return "%s first=%d last=%d len=%d" % (name, first, last, length)

    print(statmins(storage, "current"))

    if previous_leftover is not None:
        print(statmins(previous_leftover, "leftover"))
        for k in storage.keys():
            if k in previous_leftover.keys():
                lenstorage_before = len(storage[k])
                storage[k] += previous_leftover[k]
                lenstorage_after = len(storage[k])
                print("Appending %d (%d->%d) elems from previous leftover, minute=%s" %
                      (len(previous_leftover[k]), lenstorage_before,
                       lenstorage_after, k))
                del previous_leftover[k]
        for k in previous_leftover.keys():
            storage[k] = previous_leftover[k]
            print("Adding %d elems from previous leftover, minute=%s" %
                  (len(previous_leftover[k]), k))
            del previous_leftover[k]
        assert(not len(previous_leftover))

    first_minute = min(storage.keys())
    skip_firstminute = (previous_leftover is None)

#print(storage)
    for k in sorted(storage.keys()):
        #print(k)
        if k >= last_minute:
            print("Store last minute for next run %s" % k)
            leftover[k] = storage[k] # store for next run
            continue
        elif k == first_minute and skip_firstminute:
            print("Skipping first perhaps incomplete minute %s" % k) # skip first incomplete minute
            continue
        else:
            for r in storage[k]:
#    mbs['hits']:
#        tags: vhost, protocol, loc
#        value: count
#
                tags = (k, r['vhost'], r['protocol'], r['loctag'])
                mbs['hits'][tags] += 1

#    mbs['status']:
#        tags: vhost, protocol, loc,  status
#        value: count
#
                tags = (k, r['vhost'], r['protocol'], r['loctag'], r['status'])
                mbs['status'][tags] += 1

#    mbs['bytes_sent']:
#        tags: vhost, protocol, loc
#        value: sum
#
                tags = (k, r['vhost'], r['protocol'], r['loctag'])
                mbs['bytes_sent'][tags] += r['bytes_sent']


#    mbs['gzip_count']:
#        tags: vhost, protocol, loc
#        value: count
                tags = (k, r['vhost'], r['protocol'], r['loctag'])
                mbs['gzip_count'][tags] += (r['gzip_ratio'] is not None)

#    mbs['gzip_ratio']:
#        tags: vhost, protocol, loc
#        value: sum of gzip ratio / number of gzipped requests
                if r['gzip_ratio'] is not None:
                    tags = (k, r['vhost'], r['protocol'], r['loctag'])
                    mbs['_gzip_ratio_premean'][tags] += r['gzip_ratio']

#
#    mbs['request_length']:
#        tags: vhost, protocol, loc
#        value: sum of request_length / hits
#
                tags = (k, r['vhost'], r['protocol'], r['loctag'])
                mbs['_request_length_premean'][tags] += r['request_length']

#    mbs['request_time']:
#        tags: vhost, protocol, loc
#        value: sum of request_time / hits
#
                tags = (k, r['vhost'], r['protocol'], r['loctag'])
                mbs['_request_time_premean'][tags] += r['request_time']

##### upstreams

                if r['upstreams'] is not None:

#    mbs['upstreams_hits']:
#        tags: vhost, protocol, loc, upstream
#        value: count
#
                    for upstream in r['upstreams']['servers']:
                        tags = (k, r['vhost'], r['protocol'], r['loctag'], upstream)
                        mbs['upstreams_hits'][tags] += 1


#    mbs['upstreams_status']:
#        tags: vhost, protocol, loc, upstream, status
#        value: count
                    for upstream in r['upstreams']['servers']:
                        for status in r['upstreams']['status'][upstream]:
                            tags = (k, r['vhost'], r['protocol'], r['loctag'],
                                    upstream, status)
                            mbs['upstreams_status'][tags] += 1

#
#    mbs['upstreams_servers_contacted']:
#        tags: vhost, protocol, loc
#        value: sum of servers_contacted
#
                    tags = (k, r['vhost'], r['protocol'], r['loctag'])
                    mbs['upstreams_servers_contacted'][tags] += r['upstreams']['servers_contacted']


#    mbs['upstream_internal_redirects']:
#        tags: vhost, protocol, loc
#        value: sum of internal_redirects
#
                    tags = (k, r['vhost'], r['protocol'], r['loctag'])
                    mbs['upstreams_internal_redirects'][tags] += r['upstreams']['internal_redirects']


#    mbs['upstreams_servers_count']:
#        tags: vhost, protocol, loc
#        value: sum of len of servers
#
                    tags = (k, r['vhost'], r['protocol'], r['loctag'])
                    mbs['upstreams_servers'][tags] += len(r['upstreams']['servers'])


#    mbs['upstreams_response_time']:
#        tags: vhost, protocol, loc, upstream
#        value: sum of response_time / hits

                    for upstream in r['upstreams']['servers']:
                        tags = (k, r['vhost'], r['protocol'], r['loctag'],
                                    upstream)
                        mbs['_upstreams_response_time_premean'][tags] += r['upstreams']['response_time'][upstream]

#
#    mbs['upstreams_connect_time']:
#        tags: vhost, protocol, loc, upstream
#        value: sum of connect_time / hits
#
                    for upstream in r['upstreams']['servers']:
                        tags = (k, r['vhost'], r['protocol'], r['loctag'], upstream)
                        mbs['_upstreams_connect_time_premean'][tags] += r['upstreams']['connect_time'][upstream]


#    mbs['upstreams_header_time']:
#        tags: vhost, protocol, loc, upstream
#        value: sum of header_time / hits
                    for upstream in r['upstreams']['servers']:
                        tags = (k, r['vhost'], r['protocol'], r['loctag'], upstream)
                        mbs['_upstreams_header_time_premean'][tags] += r['upstreams']['header_time'][upstream]


###### calculations of means

            # gzip_ratio_mean
            if mbs['gzip_count']:
                for k, v in mbs['_gzip_ratio_premean'].items():
                    mbs['gzip_ratio_mean'][k] = v / mbs['gzip_count'][k]

            if mbs['hits']:
                # mbs['request_length_mean']
                for k, v in mbs['_request_length_premean'].items():
                    mbs['request_length_mean'][k] = v / mbs['hits'][k]

                # mbs['request_time_mean']
                for k, v in mbs['_request_time_premean'].items():
                    mbs['request_time_mean'][k] = v / mbs['hits'][k]

            if mbs['upstreams_hits']:
                # mbs['upstreams_response_time_mean']
                for k, v in mbs['_upstreams_response_time_premean'].items():
                    mbs['upstreams_response_time_mean'][k] = v / mbs['upstreams_hits'][k]

                # mbs['upstreams_connect_time_mean']
                for k, v in mbs['_upstreams_connect_time_premean'].items():
                    mbs['upstreams_connect_time_mean'][k] = v / mbs['upstreams_hits'][k]

                # mbs['upstreams_header_time_mean']
                for k, v in mbs['_upstreams_header_time_premean'].items():
                    mbs['upstreams_header_time_mean'][k] = v / mbs['upstreams_hits'][k]

###### calculations of percentage

            if mbs['hits']:
                for k, v in mbs['hits'].items():
                    if mbs['gzip_count'] and v:
                        mbs['gzip_percent'][k] = (mbs['gzip_count'][k] * 1.0) / v
                    else:
                        mbs['gzip_percent'][k] = 0.0

    for k in leftover.keys():
        print("%d %d" % (k, len(leftover[k])))
    print("Leftover = %d" % (len(leftover)))
    return (mbs, leftover)

def save_obj(obj, filepath):
    with gzip.GzipFile(filepath, 'wb') as f:
        pickle.dump(obj, f)

def load_obj(filepath):
    with gzip.GzipFile(filepath, 'rb') as f:
        return pickle.load(f)


def mbs2influx(mbs, host='x', logname='y', datacenter = ''):
    extra_tags = dict()
    points = []
    for measurement, tagnames in mbs_tags.items():
#        print(tagnames)
        for tags, value in mbs[measurement].items():
#            print(tags)
            influxtags = dict(zip(tagnames, tags[1:]))
            for k, v in influxtags.items():
                if v is None:
                    influxtags[k] = 'None'
                if k == 'protocol':
                    if v == 's':
                        influxtags[k] = 'https'
                    else:
                        influxtags[k] = 'http'
                influxtags[k] = str(v)
#            print(influxtags)
            s = str(tags[0])
            time = s[0:4]+'-'+s[4:6]+'-'+s[6:8]+'T'+s[8:10]+':'+s[10:12]+':59Z'
            fields = { 'value': value}
 #           print time
#            print fields
            points.append({
                "measurement": measurement,
                "tags": influxtags,
                "time": time,
                "fields": fields
            })
    return points


def influxdb_client(host='localhost',
                    port=8086,
                    username='root',
                    password='root',
                    database='mbstats',
                    timeout=40,
                    deletedatabase=False):
    client = InfluxDBClient(host=host,
                            port=port,
                            username=username,
                            password=password,
                            database=database,
                            timeout=timeout)
    if deletedatabase:
        client.drop_database(database)
    client.create_database(database)
    return client

def influxdb_send(client, points, tags, batch_size=200):
    npoints = len(points)
    if npoints:
        print("Sending %d points" % npoints)
        print(points[0])
        return client.write_points(points, tags=tags, time_precision='m', batch_size=batch_size)
    return True

parser = argparse.ArgumentParser()
parser.add_argument('-f', '--file')
parser.add_argument('-s', '--start', type=int)
parser.add_argument('-m', '--maxlines', default=0, type=int)
parser.add_argument('-w', '--writestatusdir', default='.')
parser.add_argument('-H', '--hostname', default=platform.node())
parser.add_argument('-l', '--logname', default='')
parser.add_argument('-d', '--datacenter', default='')
parser.add_argument('--deletedatabase', default=False)

args = parser.parse_args()
print(args)

filename = args.file

statusdir = os.path.abspath(args.writestatusdir)
sane_filename = re.sub(r'\W', '_', filename)
basepath = os.path.join(statusdir, sane_filename)
filepath_offset = basepath + '.offset'
filepath_leftover =  basepath + '.leftover'
pid = os.getpid()
fmt = "%s.%d.tmp"
filepath_offset_tmp = fmt % (filepath_offset, pid)
filepath_leftover_tmp =  fmt % (filepath_leftover, pid)
filepath_offset_old = filepath_offset + '.old'
filepath_leftover_old =  filepath_leftover + '.old'

offset_written = False
leftover_written = False

def cleanup():
    try:
        os.remove(filepath_offset_tmp)
        print("Removed %s" % filepath_offset_tmp)
    except:
        pass
    try:
        os.remove(filepath_leftover_tmp)
        print("Removed %s" % filepath_leftover_tmp)
    except:
        pass

def finalize():
    try:
        try:
            os.unlink(filepath_offset_old)
            shutil.copy2(filepath_offset, filepath_offset_old)
        except:
            pass
        try:
            os.unlink(filepath_leftover_old)
            shutil.copy2(filepath_leftover, filepath_leftover_old)
        except:
            pass
        os.rename(filepath_offset_tmp, filepath_offset)
        os.rename(filepath_leftover_tmp, filepath_leftover)
    except:
        cleanup()
        raise

res = False
try:
    influxdb = influxdb_client(deletedatabase=args.deletedatabase)

    if os.path.isfile(filepath_offset):
        shutil.copy2(filepath_offset, filepath_offset_tmp)
    pygtail = Pygtail(filename, offset_file=filepath_offset_tmp)

    storage = parsefile(pygtail, maxlines=args.maxlines)
    try:
        previous_leftover = load_obj(filepath_leftover)
    except IOError:
        previous_leftover = None
    mbs, leftover = parse_storage(storage, previous_leftover)
#print(leftover)
#leftover = json.loads(json.dumps(leftover))
    save_obj(leftover, filepath_leftover_tmp)

    points = mbs2influx(mbs)
    if points:
        tags = {
            'host': args.hostname,
            'logname': args.logname or filename,
        }
        if args.datacenter:
            tags['dc'] = args.datacenter
        influxdb_send(influxdb, points, tags)

except:
    cleanup()
    raise
else:
    finalize()
