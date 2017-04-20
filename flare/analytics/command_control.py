# coding: utf-8

import pandas as pd
from multiprocessing import Process, JoinableQueue, Lock, Manager
from elasticsearch import Elasticsearch, helpers
from flare.tools.iputils import private_check, multicast_check, reserved_check
from flare.tools.whoisip import WhoisLookup
import time
import warnings
warnings.filterwarnings('ignore')


class elasticBeacon(object):
    """
    Elastic Beacon is  designed to identify periodic communication between
    network communicatiors. Future updates will allow for dynamic fields to be passed in.

    If you do not allow your elastic search server to communicate externally, you can setup an
    ssh tunnel by using ssh -NfL 9200:localhost:9200 username@yourserver

    Otherwise, you'll need to adjust es_host to the IP address that is exposed to elasticSearch.

    """
    def __init__(self,
                 min_occur=10,
                 min_percent=5,
                 window=2,
                 threads=8,
                 period=24,
                 es_host='localhost',
                 es_timeout=480,
                 kibana_version='5',
                 verbose=True):
        """

        :param min_occur: Minimum number of triads to be considered beaconing
        :param min_percent: Minimum percentage of all connection attempts that
         must fall within the window to be considered beaconing
        :param window: Size of window in seconds in which we group connections to determine percentage, using a
         large window size can give inaccurate interval times, multiple windows contain all interesting packets,
         so the first window to match is the interval
        :param threads: Number of cores to use
        :param period: Number of hours to locate beacons for
        :param es_host: IP Adddress of elasticsearch host (default is localhost)
        :param es_timeout: Sets timeout to 480 seconds
        :param kibana_version: 4 or 5 (query will depend on version)
        """
        self.MIN_OCCURANCES = min_occur
        self.MIN_PERCENT = min_percent
        self.WINDOW = window
        self.NUM_PROCESSES = threads
        self.period = period
        self.kibana_version = kibana_version
        self.ver = {'4': {'filtered': 'query'}, '5': {'bool': 'must'}}
        self.filt = self.ver[self.kibana_version].keys()[0]
        self.query = self.ver[self.kibana_version].values()[0]
        self.verbose = verbose
        self.whois = WhoisLookup()
        self.fields = ['src_ip', 'dest_ip', 'dest_port', 'bytes_toserver','dest_degree', 'percent', 'interval']

        try:
            self.es = Elasticsearch(es_host, timeout=es_timeout)
        except Exception as e:
            raise Exception(
                "Could not connect to ElasticSearch -- Please verify your settings are correct and try again.")

        self.q_job = JoinableQueue()
        self.l_df = Lock()
        self.l_list = Lock()
        self.high_freq = None
        self.flow_data = self.run_query()

    def hour_query(self, h, *fields):
        """

        :param h: Number of hours to look for beaconing (recommend 24 if computer can support it)
        :param fields: Retrieve only these fields -- example "src_ip", "dest_ip", "src_port", "dest_port"
        :return:
        """
        # Timestamp in ES is in milliseconds
        NOW = int(time.time() * 1000)
        SECONDS = 1000
        MINUTES = 60 * SECONDS
        HOURS = 60 * MINUTES
        lte = NOW
        gte = int(NOW - h * HOURS)

        query = {
            "query": {
                self.filt: {
                    self.query: {
                        "query_string": {
                            "query": "*",
                            "analyze_wildcard": 'true'
                        }
                    },
                    "filter": {
                        "bool": {
                            "must": [
                                {
                                    "range": {
                                        "timestamp": {
                                            "gte": gte,
                                            "lte": lte,
                                            "format": "epoch_millis"
                                        }
                                    }
                                }
                            ],
                            "must_not": []
                        }
                    }
                }
            }
        }
        if fields:
            query["_source"] = list(fields)

        return query

    def percent_grouping(self, d):
        mx = 0
        mx_key = 0
        total = 0
        interval = 0

        for key in d.keys():
            total += d[key]

            if d[key] > mx:
                mx = d[key]
                mx_key = key

        # if our high count interval is less than 10 we don't evaluate
        if mx_key < 10:
            return 0, 0.0
        else:
            mx_percent = 0.0
            for i in range(mx_key - self.WINDOW, mx_key + 1):
                current = 0
                # Finding center of current window
                curr_interval = i + int(self.WINDOW / 2)
                for j in range(i, i + self.WINDOW):
                    if d.has_key(j):
                        current += d[j]
                percent = float(current) / total * 100

                if percent > mx_percent:
                    mx_percent = percent
                    interval = curr_interval

        return interval, mx_percent

    def run_query(self):
        if self.verbose:
            print("[+] Gathering flow data... this may take a while...")
        query = self.hour_query(self.period, "src_ip", "dest_ip", "dest_port", "@timestamp", "flow.bytes_toserver",
                                "flow_id")
        resp = helpers.scan(query=query, client=self.es, scroll="90m", index="logstash-flow*", timeout="10m")
        df = pd.DataFrame([rec['_source'] for rec in resp])
        df['dest_port'] = df['dest_port'].fillna(0).astype(int)
        df.flow = df.flow.apply(lambda x: x.get('bytes_toserver'))
        df['triad_id'] = (df['src_ip'] + df['dest_ip'] + df['dest_port'].astype(str)).apply(hash)
        df['triad_freq'] = df.groupby('triad_id')['triad_id'].transform('count').fillna(0).astype(int)
        self.high_freq = df[df.triad_freq > self.MIN_OCCURANCES].groupby('triad_id').groups.keys()
        return df

    def find_beacon(self, q_job, beacon_list):

        while not q_job.empty():
            triad_id = q_job.get()
            self.l_df.acquire()

            work = self.flow_data[self.flow_data.triad_id == triad_id]
            self.l_df.release()
            work['delta'] = (pd.to_datetime(work['@timestamp']).astype(int) / 1000000000 -
                             pd.to_datetime(work['@timestamp']).shift().astype(int) / 1000000000).astype(int)

            d = dict(work.delta.value_counts())

            window, percent = self.percent_grouping(d)

            if percent > self.MIN_PERCENT:
                PERCENT = str(int(percent))
                WINDOW = str(window)
                SRC_IP = work.src_ip.unique()[0]
                DEST_IP = work.dest_ip.unique()[0]
                DEST_PORT = str(int(work.dest_port.unique()[0]))
                BYTES_TOSERVER = work.flow.unique()[0]
                SRC_DEGREE = len(work.dest_ip.unique())
                self.l_list.acquire()
                beacon_list.append([SRC_IP, DEST_IP, DEST_PORT, BYTES_TOSERVER, SRC_DEGREE, PERCENT, WINDOW])
                self.l_list.release()
            q_job.task_done()

    def find_beacons(self, group=True, focus_outbound=False, whois=True, csv_out=None):
        for triad_id in self.high_freq:
            self.q_job.put(triad_id)

        mgr = Manager()
        beacon_list = mgr.list()
        processes = [Process(target=self.find_beacon, args=(self.q_job, beacon_list,)) for thread in
                     range(self.NUM_PROCESSES)]

        # Run processes
        for p in processes:
            p.start()

        # Exit the completed processes
        for p in processes:
            p.join()

        beacon_list = list(beacon_list)
        beacon_df = pd.DataFrame(beacon_list,
                                 columns=self.fields)
        beacon_df.interval = beacon_df.interval.astype(int)
        beacon_df['dest_degree'] = beacon_df.groupby('dest_ip')['dest_ip'].transform('count').fillna(0).astype(int)
        if whois:
            beacon_df['src_whois'] = beacon_df['src_ip'].apply(lambda ip: self.whois.get_name_by_ip(ip))
            beacon_df['dest_whois'] = beacon_df['dest_ip'].apply(lambda ip: self.whois.get_name_by_ip(ip))

        if focus_outbound:
            beacon_df = beacon_df[(beacon_df.src_ip.apply(private_check)) &
                                        (~beacon_df.dest_ip.apply(multicast_check)) &
                                        (~beacon_df.dest_ip.apply(reserved_check)) &
                                        (~beacon_df.dest_ip.apply(private_check))]
        if csv_out:
            beacon_df.to_csv(csv_out)
        if group:

            self.fields.insert(self.fields.index('dest_ip'), 'dest_whois')
            beacon_df = pd.DataFrame(beacon_df.groupby(
                self.fields).size())
            beacon_df.drop(0, axis=1, inplace=True)
        return beacon_df