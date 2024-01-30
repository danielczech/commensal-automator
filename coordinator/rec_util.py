import threading
import time
import numpy as np
import json
from datetime import datetime, timedelta

from coordinator import util, redis_util
from coordinator.logger import log
from coordinator.telstate_interface import TelstateInterface

HPGDOMAIN = 'bluse'
PKTIDX_MARGIN = 2048 # in packets
TARGETS_CHANNEL = 'target-selector:new-pointing'
DEFAULT_DWELL = 290

def record(r, array, instances):
    """Start and check recording for a non-primary time track.

    Calibration solutions are retrieved, formatted in the background
    after a 60 second delay and saved to Redis. This 60 second delay
    is needed to ensure that the calibration solutions provided by
    Telstate are current.
    """

    # Attempt to get current target information:
    target_data = util.retry(5, 5, get_primary_target, r, array, 16, "|")
    if not target_data:
        log.error(f"Could not retrieve current target for {array}")
        return

    # Retrieve calibration solutions after 60 seconds have passed (see above
    # for explanation of this delay):
    delay = threading.Timer(60, lambda:get_cals(r, array))
    log.info("Starting delay to retrieve cal solutions in background")
    delay.start()

    # Supply Hashpipe-Redis gateway keys to the instances which will conduct
    # recording:

    # Set DWELL in preparation for recording:
    redis_util.set_group_key(r, array, "DWELL", DEFAULT_DWELL)

    # Calculate PKTSTART:
    pktstart_data = get_pktstart(r, instances, PKTIDX_MARGIN, array)
    if not pktstart_data:
        log.error(f"Could not calculate PKTSTART for {array}")
        return
    pktstart = pktstart_data["pktstart"]
    pktstart_ts = pktstart_data["pktstart_ts"]
    pktstart_str = pktstart_data["pktstart_str"]

    # Retrieve fecenter:
    fecenter = centre_freq(r, array)
    if not fecenter:
        log.error(f"Could not retreive FECENTER for {array}")
        return

    # DATADIR
    sb_id = redis_util.sb_id(r, array)
    set_datadir(r, array, pktstart_str, [0,1], sb_id)

    # SRC_NAME:
    redis_util.set_group_key(r, array, "SRC_NAME", target_data["target"])

    # RA and Dec at start of observation:
    ra_d = util.ra_degrees(target_data["ra"])
    redis_util.set_group_key(r, array, "RA", ra_d)
    redis_util.set_group_key(r, array, "RA_STR", target_data["ra"])

    dec_d = util.dec_degrees(target_data["dec"])
    redis_util.set_group_key(r, array, "DEC", dec_d)
    redis_util.set_group_key(r, array, "DEC_STR", target_data["dec"])

    # OBSID (unique identifier for a particular observation):
    obsid = f"MeerKAT:{array}:{pktstart_str]}"
    redis_util.set_group_key(r, array, "OBSID", obsid)

    # Set PKTSTART separately after all the above messages have
    # all been delivered:
    redis_util.set_group_key(r, array, "PKTSTART", str(pktstart))

    # Grafana annotation that recording has started:
    annotate('RECORD', f"{array}, OBSID: {obsid}")

    # Alert the target selector to the new pointing:
    request_targets(r, array, pktstart_ts, target_data["target"], ra_d, dec_d)

    # Check if this recording is primary time:
    if check_primary_time(r, array):
        log.info("Primary time detected.")
        redis_util.alert(r,
        f":zap: `{array}` Primary time detected, human intervention required after recording",
        "coordinator")
    else:
        # Write datadir to the list of unprocessed directories for this subarray:
        add_unprocessed(r, set(instances), pktstart_str, sb_id):

    # Write metadata for current obsid:
    write_metadata(r, instance, pktstart_ts, obsid, DEFAULT_DWELL, datadir, array)

    # Start recording timeout timer, with 10 second safety margin:
    rec_timer = threading.Timer(300, lambda:timeout(r, array, "rec_result"))
    log.info("Starting recording timeout timer.")
    rec_timer.start()

    redis_util.alert(r,
        f":black_circle_for_record: `{array}` recording: `{obsid}`",
        "coordinator")
    # If this is primary time, write datadir to the list of directories to
    # preserve:
    # TODO: check for primary time first
    #if is_primary_time():
    #    add_preserved(r, recording, datadir)

    # TODO: check recording here
    # Wait half a second to ensure recording has started:
    # time.sleep(0.5)
    # recording = get_recording(r, instances)

    return set(instances)

def write_metadata(r, instance, pktstart_ts, dwell, datadir, array):
    """Write current rec info so that other processes (e.g. analyzer) can
    make requests for new targets.
    """
    nants = r.llen(f"{array}:antennas")
    band = obs_band(r, array)
    current_rec_data = {
        "band":band,
        "start_ts":pktstart_ts,
        "nants":nants,
        "obsid":obsid
    }
    # Link subarray (current datadir associated with <array>):
    r.set(f"{array}:datadir", datadir)
    # write metadata
    r.set(f"metadata:{datadir}", json.dumps(current_rec_data))
    # Write predicted stop time:
    r.set(f"rec_end:{datadir}", pktstart_ts + dwell)

def request_targets(r, array, pktstart_ts, target, ra_deg, dec_deg):
    """Request a new target list to be generated by the target selector.
    ra and dec in degrees, f_max in MHz.
    """
    band = obs_band(r, array)
    f_max = centre_freq(r, array) + bandwidth(r, array)/2
    details = {
        "telescope":"MeerKAT",
        "array":array,
        "pktstart_ts":pktstart_ts,
        "target":target,
        "ra_deg":ra_deg,
        "dec_deg":dec_deg,
        "f_max":f_max,
        "band":band
    }
    msg = f"POINTING:{json.dumps(details)}"
    r.publish(TARGETS_CHANNEL, msg)


def obs_band(r, array):
    """Get the current observing band.
    """
    # TODO: autodetect band based on center freq for S-band
    sensor = f"subarray_{array[-1]}_band"
    return r.get(sensor)


def centre_freq(r, array):
    """Centre frequency (FECENTER).
    """
    try:
        # build the specific sensor ID for retrieval
        s_num = array[-1] # subarray number
        sensor = "antenna_channelised_voltage_centre_frequency"
        cbf_prefix = r.get(f"{array}:cbf_prefix")
        sensor_key = f"{array}:subarray_{s_num}_streams_{cbf_prefix}_{sensor}"
        centre_freq = r.get(sensor_key)
        centre_freq = float(centre_freq)/1e6
        centre_freq = '{0:.17g}'.format(centre_freq)
        return centre_freq
    except Exception as e:
        log.error(e)

def bandwidth(r, array):
    """Get the current observing bandwidth in MHz.
    """
    try:
        cbf_prefix = r.get(f"{array}:cbf_prefix")
        sensor = f"cbf_{array[-1]}_{cbf_prefix}_bandwidth"
        bandwidth = r.get(sensor)
        return float(bandwidth)/1e6
    except Exception as e:
        log.error(e)

def check_primary_time(r, array):
    """Check if the current recording is primary time.
    """
    array_num = array[-1] # last char is array number
    key = f"{array}:subarray_{array_num}_script_proposal_id"
    proposal_id = r.get(key)
    if not proposal_id:
        return
    log.info(f"Retrieved currrent proposal ID: {proposal_id}")
    # This is the current active proposal ID to look for. If it
    # is detected, we want to enter the "waiting" state.
    if proposal_id.strip("'") == "DDT-20230920-DC-01":
        return True

def set_datadir(r, array, pktstart_str, instance_numbers, sb_id):
    """Set DATADIR correctly for each instance. For each host, instance 0
    must always use `/buf0` and instance 1 must always use `/buf1`.
    """
    for instance_n in instance_numbers:
        group = f"{HPGDOMAIN}:{array}-{instance_n}///set"
        datadir = f"/buf{instance_n}/{pktstart_str}-{sb_id}"
        redis_util.gateway_msg(r, group, 'DATADIR', datadir, False)


def add_unprocessed(r, recording, pktstart_str, sb_id):
    """Set the list of unprocessed directories.
    """
    log.info(f"Adding datadir to <instance>:unprocessed")
    for instance in recording:
        host, n = instance.split("/")
        datadir = f"/buf{n}/{pktstart_str}-{sb_id}"
        r.lpush(f"{instance}:unprocessed", datadir)


def add_preserved(r, recording, datadir):
    """Set the list of unprocessed directories.
    """
    for instance in recording:
        r.lpush(f"{instance}:preserved", datadir)


def get_primary_target(r, array, length, delimiter = "|"):
    """Attempt to determine the current track's target. 
    
    Belt-and-braces approach:
    Compare target value timestamp with the timestamp of the end
    of the last track. If the target value was last updated during
    the preceding track, it is stale and we should not procede.
    
    Parse target string and extract name, RA and dec. Format target for
    compatibility with filterbank/raw file header requirements. All contents 
    up to the stop character are kept.

    A typical target description string from CBF: 
        "J0918-1205 | Hyd A | Hydra A | 3C 218 | PKS 0915-11, radec, 
        9:18:05.28, -12:05:48.9"
    
    length (int): Maximum length for target description.
    delimiter (str): Character at which to split the target string. 
    
    Returns:
        target: Formatted target description suitable for 
        filterbank/raw headers.
        ra_str: RA_STR as accessed from target string.
        dec_str: DEC_STR as accessed from target string.
    """ 
    
    target_val = r.get(f"{array}:target")
    target_ts = float(r.get(f"{array}:last-target")) 
    last_track_end = float(r.get(f"{array}:last-track-end"))
    log.info(f"Target: {target_val}, ts: {target_ts}, last: {last_track_end}")
    # Until we figure out new CAM target delivery: accept a target if it is
    # newer than `last_track_end - 5`
    if target_ts < last_track_end - 5:
        log.warning(f"No target data yet for current track for {array}.")
        return
    # Assuming target name or description will always come first
    # Remove any outer single quotes for compatibility:
    target = target_val.strip('\'')
    if 'radec' in target:
        target = target.split(',') 
        # Check if target name or description present
        if len(target) < 4: 
            log.warning("Target name not provided.")
            target_name = 'NOT_PROVIDED'
            ra_str = target[1].strip()
            dec_str = target[2].strip()
        else:
            target_name = target[0].split(delimiter)[0] 
            target_name = target_name.strip() 
            target_name = target_name.strip(",") 
            # Note that + and - are not removed 
            punctuation = "!\"#$%&\'()*,./:;<=>?@[\\]^_`{|}~" 
            # Replace all punctuation with underscores
            table = str.maketrans(punctuation, '_'*30)
            target_name = target_name.translate(table)
            # Limit target string to max allowable in headers (68 chars)
            target_name = target_name[0:length]
            ra_str = target[2].strip()
            dec_str = target[3].strip()
        return {"target":target_name, "ra":ra_str, "dec":dec_str}
    else:
        # We are unsure of target format since no radec field provided. 
        log.warning(f"Target name and description incomplete for {array}.")
        return

def get_cals(r, array):
    """Retrieves calibration solutions and saves them to Redis. They are
    also formatted and indexed.
    """
    # Retrieve current telstate endpoint:
    endpoint_key = r.get(f"{array}:telstate_sensor")
    endpoint_val = r.get(endpoint_key)
    # Parse endpoint. Arrives as string in specific format:
    # e.g. "('10.98.2.128', 31029)"
    try:
        components = endpoint_val.strip("()").split(",")
        ip = components[0].strip("'")
        port = components[1].strip()
        telstate_endpoint = f"{ip}:{port}"
    except:
        log.error(f"Could not parse Telstate endpoint: {endpoint_val}")
        return
    # Initialise telstate interface object
    try:
        TelInt = TelstateInterface(r, telstate_endpoint)
    except Exception as e:
        log.error("Could not connect to TelState, details follow:")
        log.error(e)
        return
    # Before requesting solutions, check first if they have been delivered
    # since this subarray was last configured:
    last_config_ts = r.get(f"{array}:last-config") # last config ts
    if not last_config_ts:
        log.warning("No key set for last_config_ts.")
        last_config_ts = 0
    else:
        last_config_ts = float(last_config_ts)
    current_cal_ts = TelInt.get_phaseup_time() # current cal ts
    if current_cal_ts < last_config_ts:
        log.warning(f"Calibration solutions not yet available for {array}")
        return

    # Next, check if they are newer than the most recent set that was
    # retrieved. Note that a set is always requested if this is the
    # first recording for a particular subarray configuration.
    last_cal_ts = r.get(f"{array}:last-cal")
    if not last_cal_ts:
        log.warning("No key set for last_cal_ts.")
        last_cal_ts = 0
    else:
        last_cal_ts = float(last_cal_ts)
    if last_cal_ts < current_cal_ts:
        # Retrieve and save calibration solutions:
        TelInt.query_telstate(array)
        log.info(f"New calibration solutions retrieved for {array}")
        r.set(f"{array}:last-cal", current_cal_ts)
        return "success"
    else:
        log.info("No calibration solution updates")


def get_pktstart(r, instances, margin, array):
    """Calculate PKTSTART for specified DAQ instances.
    """

    # Get current packet indices for each instance:
    pkt_indices = []
    for instance in instances:
        key = f"{HPGDOMAIN}://{instance}/status"
        pkt_index = get_pkt_idx(r, key)
        if not pkt_index:
            continue
        pkt_indices.append(pkt_index)

    # Calculate PKTSTART
    if len(pkt_indices) > 0:

        pkt_indices = np.asarray(pkt_indices, dtype = np.int64)
        max_ts = redis_util.pktidx_to_timestamp(r, np.max(pkt_indices), array)
        med_ts = redis_util.pktidx_to_timestamp(r, np.median(pkt_indices), array)
        min_ts = redis_util.pktidx_to_timestamp(r, np.min(pkt_indices), array)

        pktstart = np.max(pkt_indices) + margin

        pktstart_timestamp = redis_util.pktidx_to_timestamp(r, pktstart, array)
        pktstart_dt = datetime.utcfromtimestamp(pktstart_timestamp)
        pktstart_str = pktstart_dt.strftime("%Y%m%dT%H%M%SZ")

        log.info(f"PKTIDX: Min {min_ts}, Med {med_ts}, Max {max_ts}, PKTSTART {pktstart_timestamp}")

        # Check that calculated pktstart is plausible:
        if abs(pktstart_dt - datetime.utcnow()) > timedelta(minutes=2):
            log.warning(f"bad pktstart: {pktstart_str} for {array}")
            redis_util.alert(r,
                f":warning: `{array}` bad pktstart",
                "coordinator")
            return

        return {
            "pktstart":pktstart,
            "pktstart_str":pktstart_str,
            "pktstart_ts":pktstart_timestamp
            }
    else:
        log.warning(f"Could not retrieve PKTIDX for {array}")


def get_pkt_idx(r, instance_key):
    """Get PKTIDX for an HPGUPPI_DAQ instance.

    Returns:
        pkt_idx (str): Current packet index (PKTIDX) for a particular
        active host. Returns None if host is not active.
    """
    pkt_idx = None
    # get the status hash from the DAQ instance
    daq_status = r.hgetall(instance_key)
    if len(daq_status) > 0:
        if 'NETSTAT' in daq_status:
            if daq_status['NETSTAT'] != 'idle':
                if 'PKTIDX' in daq_status:
                    pkt_idx = daq_status['PKTIDX']
                else:
                    log.warning(f"PKTIDX is missing for {instance_key}")
        else:
            log.warning(f"NETSTAT is missing for {instance_key}")
    else:
        log.warning(f"Cannot acquire {instance_key}")
    return pkt_idx

def annotate(tag, text):
    response = util.annotate_grafana(tag, text)
    log.info(f"Annotating Grafana, response: {response}")

def get_recording(r, instances):
    """Check if given instances are recording.
    """
    ins = redis_util.multiget_by_instance(r, HPGDOMAIN, instances, "DAQSTATE")
    return set([inst[0] for inst in ins if inst[1][0] == "RECORD"])


def timeout(r, array, channel):
    """Temporary timeout mechanism for recordings.
    """
    r.publish(channel, f"rec-timeout:{array}")
