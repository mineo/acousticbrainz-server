#!/usr/bin/env python

import sys
sys.path.append("../acousticbrainz")

import urllib2
import json
import os
from time import sleep
import subprocess
from operator import itemgetter
import psycopg2
import config
from threading import Thread
import random
from hashlib import sha256, sha1
import tempfile
import logging
from logging.handlers import RotatingFileHandler

MAX_THREADS = 4
SLEEP_DURATION = 30 # number of seconds to wait between runs
HIGH_LEVEL_EXTRACTOR_BINARY = "streaming_extractor_music_svm"

PROFILE_CONF_TEMPLATE = "profile.conf.in"
PROFILE_CONF = "profile.conf"
PROFILE_SHA1_PATTERN = "@EXTRACTOR_BINARY_SHA@"

logging.basicConfig(format='%(asctime)s: %(message)s', level=logging.INFO)
handler = RotatingFileHandler(config.HL_LOG_FILE, maxBytes=config.LOG_SIZE, backupCount=config.LOG_FILES_SAVED)
handler.setLevel(logging.INFO)
log = logging.getLogger('hl_analysis')
log.addHandler(handler)

class HighLevel(Thread):
    """
        This thread class calculates the high level data by calling the external high level calculator
    """

    def __init__(self, mbid, ll_data, ll_id):
        Thread.__init__(self)
        self.mbid = mbid
        self.ll_data = ll_data
        self.hl_data = None
        self.ll_id = ll_id

    def _calculate(self):
        """
           Invoke essentia high level extractor and return its JSON output
        """

        try:
            f = tempfile.NamedTemporaryFile(delete=False)
            name = f.name
            f.write(self.ll_data)
            f.close()
        except IOError:
            log.info("IO Error while writing temp file")
            return "{}"

        # Securely generate a temporary filename
        tmp_file = tempfile.mkstemp()
        out_file = tmp_file[1]
        os.close(tmp_file[0])

        fnull = open(os.devnull, 'w')
        try:
            subprocess.check_call([os.path.join(".", HIGH_LEVEL_EXTRACTOR_BINARY), name, out_file, PROFILE_CONF], stdout=fnull, stderr=fnull)
        except subprocess.CalledProcessError:
            log.info("Cannot call high level extractor")
            return "{}"

        fnull.close()
            
        try:
            f = open(out_file)
            hl_data = f.read()
            f.close()
            os.unlink(out_file)
        except IOError:
            log.info("IO Error while removing temp file")
            return "{}"

        return hl_data

    def get_data(self):
        return self.hl_data

    def get_ll_id(self):
        return self.ll_id

    def run(self):
        self.hl_data = self._calculate()

def get_documents(conn):
    """
        Fetch a number of low level documents to process from the DB
    """
    cur = conn.cursor()
    cur.execute("""SELECT ll.mbid, ll.data, ll.id
                     FROM lowlevel AS ll 
                LEFT JOIN highlevel AS hl 
                       ON ll.id = hl.id 
                    WHERE hl.mbid IS NULL
                    LIMIT 100""")
    docs = cur.fetchall()
    cur.close()
    return docs

def create_profile(in_file, out_file, sha1):
    try:
        f = open(in_file)
        profile = f.read()
        f.close()
    except IOError, e:
        log.info("Cannot read profile template %s: %s" % (in_file, e))
        sys.exit(-1)

    profile = profile.replace(PROFILE_SHA1_PATTERN, sha1)

    try:
        f = open(out_file, "w")
        f.write(profile)
        f.close()
    except IOError, e:
        log.info("Cannot write profile %s: %s" % (out_file, e))
        sys.exit(-1)

def get_build_sha1(binary):
    """
        Calculate the sha1 of the binary we're using.
    """
    try:
        f = open(binary, "r")
        bin = f.read()
        f.close()
    except IOError, e:
        log.info("Cannot calculate the SHA256 of the high level binary: %s" % e)
        sys.exit(-1)

    return sha1(bin).hexdigest() 

build_sha1 = get_build_sha1(HIGH_LEVEL_EXTRACTOR_BINARY)
create_profile(PROFILE_CONF_TEMPLATE, PROFILE_CONF, build_sha1)
    
conn = None
num_processed = 0

pool = {}
docs = []
while True:
    # Check to see if we need more database rows
    if len(docs) == 0:
        # Fetch more rows from the DB
        if not conn:
            conn = psycopg2.connect(config.PG_CONNECT)
        docs = get_documents(conn)

        # We will fetch some rows that are already in progress. Remove those.
        in_progress = pool.keys()
        filtered = []
        for mbid, doc, id in docs:
            if not mbid in in_progress:
                filtered.append((mbid, doc, id))
        docs = filtered

    if len(docs):
        # Start one document
        mbid, doc, id = docs.pop()
        th = HighLevel(mbid, doc, id)
        th.start()
        log.info("start %s" % mbid)
        pool[mbid] = th

    # If we're at max threads, wait for one to complete
    while True:
        if len(pool) == 0 and len(docs) == 0:
            log.info("processed %s documents, none remain. Sleeping." % num_processed)
            num_processed = 0
            # Let's be nice and not keep any connections to the DB open while we nap
            conn.close()
            conn = None
            sleep(SLEEP_DURATION)

        for mbid in pool.keys():
            if not pool[mbid].is_alive():

                # Fetch the data and clean up the thread object
                hl_data = pool[mbid].get_data()
                ll_id = pool[mbid].get_ll_id()
                pool[mbid].join()
                del pool[mbid]

                # Calculate the sha for the data
                try:
                    jdata = json.loads(hl_data)
                except ValueError:
                    log.info("error %s: Cannot parse result document" % mbid)
                    log.info(hl_data)
                    jdata = {}

                norm_data = json.dumps(jdata, sort_keys=True, separators=(',', ':'))
                sha = sha256(norm_data).hexdigest()

                log.info("done  %s" % mbid)
                if not conn:
                    conn = psycopg2.connect(config.PG_CONNECT)

                cur = conn.cursor()
                cur.execute("""INSERT INTO highlevel_json (data, data_sha256) 
                                    VALUES (%s, %s) 
                                 RETURNING id""", (norm_data, sha))
                id = cur.fetchone()[0]
                cur.execute("""INSERT INTO highlevel (id, mbid, build_sha1, data, submitted)
                                    VALUES (%s, %s, %s, %s, now())""", (ll_id, mbid, build_sha1, id))
                conn.commit()
                num_processed += 1

        if len(pool) == MAX_THREADS:
            # tranquilo!
            sleep(.1)
        else:
            break