#!/usr/bin/env python

# General helper functions used by the coordinator 

import zmq
import os
import requests
import json
import time
import yaml

from automator.logger import log

GRAFANA_ANNOTATIONS_URL = "http://blh0:3000/api/annotations"
GRAFANA_AUTH = os.environ['GRAFANA_AUTH']

def config(cfg_file):
    """Configure the coordinator according to .yml config file.
    Returns list of instances and the number of streams to be processed per
    instance.
    """
    try:
        with open(cfg_file, 'r') as f:
            try:
                cfg = yaml.safe_load(f)
                return cfg
            except yaml.YAMLError as e:
                log.error(e)
    except IOError:
        log.error('Could not open config file.')

def restart_pipeline(hosts, pipeline):
    """Use ZMQ to restart pipelines on deconfigure to ensure they have
    correctly unsubscribed.
    """
    command = {
        "command":"restart",
        "properties":{
            "name":pipeline,
            "waiting":False,
            "match":"simple"
            }
        }
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.DEALER)
    failed = []
    for host in hosts:
        s.connect(f"tcp://{host}:5555")
        s.send_json(command)
        r = s.recv_json()
        if r['status'] != 'ok':
            # log warning/error
            failed.append(host)
        s.disconnect(f"tcp://{host}:5555")
    return failed


def annotate_grafana(tag, text, url=GRAFANA_ANNOTATIONS_URL, auth=GRAFANA_AUTH):
    """Create Grafana annotations.

    Args:
        tag (str): Grafana tag
        text (str): Associated description text
        url (str): Grafana annotations URL
        auth (str): Grafana auth token

    Returns:
        http POST response
    """
    header = {
        "Authorization":"Bearer {}".format(auth),
        "Accept":"application/json",
        "Content-Type":"application/json"
    }

    annotation = {
        "time":int(time.time()*1000), # Unix epoch, UTC in milliseconds
        "isRegion":False,
        "tags":[tag],
        "text":text,
    }

    return requests.post(
        url,
        headers=header,
        data=json.dumps(annotation)
    )

def retry(retries, delay, function, *args):
    """Generic retries. Will retry if function returns None.
    Returns None if unsuccessful.
    """
    for _ in range(retries):
        try:
            output = function(*args)
            if not output:
                time.sleep(delay)
                continue
            return output
        except Exception as e:
            log.error(f"Exception: {e}")
            continue
    log.error(f"Unsuccessful after {retries} retries.")

def dec_degrees(dec_s):
    """Convert RA from sexagesimal form to degree form.
    """
    dec = dec_s.split(':')
    d = int(dec[0])
    m = int(dec[1])
    s = float(dec[2])
    if dec[0][0] == '-':
        dec_d = d - m/60.0 - s/3600.0
    else:
        dec_d = d + m/60.0 + s/3600.0
    return dec_d

def ra_degrees(ra_s):
    """Convert RA from sexagesimal form to degree form.
    """
    ra = ra_s.split(':')
    h = int(ra[0])
    m = int(ra[1])
    s = float(ra[2])
    return h*15 + m*0.25 + s*15.0/3600.0