#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
Search Architecture:
 - Have a list of accounts
 - Create an "overseer" thread
 - Search Overseer:
   - Tracks incoming new location values
   - Tracks "paused state"
   - During pause or new location will clears current search queue
   - Starts search_worker threads
 - Search Worker Threads each:
   - Have a unique API login
   - Listens to the same Queue for areas to scan
   - Can re-login as needed
   - Shares a global lock for map parsing
'''

import logging
import math
import json
import os
import random
import time
import geopy
import geopy.distance

from operator import itemgetter
from threading import Thread
from queue import Queue, Empty

from pgoapi import PGoApi
from pgoapi.utilities import f2i
from pgoapi import utilities as util
from pgoapi.exceptions import AuthException

from .models import parse_map, Pokemon
from .fakePogoApi import FakePogoApi
import terminalsize

log = logging.getLogger(__name__)

TIMESTAMP = '\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000'


def get_new_coords(init_loc, distance, bearing):
    """
    Given an initial lat/lng, a distance(in kms), and a bearing (degrees),
    this will calculate the resulting lat/lng coordinates.
    """
    R = 6378.1  # km radius of the earth
    bearing = math.radians(bearing)

    init_coords = [math.radians(init_loc[0]), math.radians(init_loc[1])]  # convert lat/lng to radians

    new_lat = math.asin(math.sin(init_coords[0]) * math.cos(distance / R) +
                        math.cos(init_coords[0]) * math.sin(distance / R) * math.cos(bearing)
                        )

    new_lon = init_coords[1] + math.atan2(math.sin(bearing) * math.sin(distance / R) * math.cos(init_coords[0]),
                                          math.cos(distance / R) - math.sin(init_coords[0]) * math.sin(new_lat)
                                          )

    return [math.degrees(new_lat), math.degrees(new_lon)]


def generate_location_steps(initial_loc, step_count, step_distance):
    # Bearing (degrees)
    NORTH = 0
    EAST = 90
    SOUTH = 180
    WEST = 270

    pulse_radius = step_distance            # km - radius of players heartbeat is 70m
    xdist = math.sqrt(3) * pulse_radius   # dist between column centers
    ydist = 3 * (pulse_radius / 2)          # dist between row centers

    yield (initial_loc[0], initial_loc[1], 0)  # insert initial location

    ring = 1
    loc = initial_loc
    while ring < step_count:
        # Set loc to start at top left
        loc = get_new_coords(loc, ydist, NORTH)
        loc = get_new_coords(loc, xdist / 2, WEST)
        for direction in range(6):
            for i in range(ring):
                if direction == 0:  # RIGHT
                    loc = get_new_coords(loc, xdist, EAST)
                if direction == 1:  # DOWN + RIGHT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist / 2, EAST)
                if direction == 2:  # DOWN + LEFT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist / 2, WEST)
                if direction == 3:  # LEFT
                    loc = get_new_coords(loc, xdist, WEST)
                if direction == 4:  # UP + LEFT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist / 2, WEST)
                if direction == 5:  # UP + RIGHT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist / 2, EAST)
                yield (loc[0], loc[1], 0)
        ring += 1


# Apply a location jitter
def jitterLocation(location=None, maxMeters=10):
    origin = geopy.Point(location[0], location[1])
    b = random.randint(0, 360)
    d = math.sqrt(random.random()) * (float(maxMeters) / 1000)
    destination = geopy.distance.distance(kilometers=d).destination(origin, b)
    return (destination.latitude, destination.longitude, location[2])


# gets the current time past the hour
def curSec():
    return (60 * time.gmtime().tm_min) + time.gmtime().tm_sec


# gets the diference between two times past the hour (in a range from -1800 to 1800)
def timeDif(a, b):
    dif = a - b
    if (dif < -1800):
        dif += 3600
    if (dif > 1800):
        dif -= 3600
    return dif


# binary search to get the lowest index of the item in Slist that has atleast time T
def SbSearch(Slist, T):
    first = 0
    last = len(Slist) - 1
    while first < last:
        mp = (first + last) // 2
        if Slist[mp]['time'] < T:
            first = mp + 1
        else:
            last = mp
    return first


# Thread to handle user input
def switch_status_printer(display_enabled, current_page):
    while True:
        # Wait for the user to press a key
        command = raw_input()

        if command == '':
            # Switch between logging and display.
            if display_enabled[0]:
                logging.disable(logging.NOTSET)
                display_enabled[0] = False
            else:
                logging.disable(logging.ERROR)
                display_enabled[0] = True
        elif command.isdigit():
                current_page[0] = int(command)


# Thread to print out the status of each worker
def status_printer(threadStatus, search_items_queue, db_updates_queue, wh_queue):
    display_enabled = [True]
    current_page = [1]
    logging.disable(logging.ERROR)

    # Start another thread to get user input
    t = Thread(target=switch_status_printer,
               name='switch_status_printer',
               args=(display_enabled, current_page))
    t.daemon = True
    t.start()

    while True:
        if display_enabled[0]:

            # Get the terminal size
            width, height = terminalsize.get_terminal_size()
            # Queue and overseer take 2 lines.  Switch message takes up 2 lines.  Remove an extra 2 for things like screen status lines.
            usable_height = height - 6
            # Prevent people running terminals only 6 lines high from getting a divide by zero
            if usable_height < 1:
                usable_height = 1

            # Create a list to hold all the status lines, so they can be printed all at once to reduce flicker
            status_text = []

            # Print the queue length
            status_text.append('Queues: {} items, {} db updates, {} webhook'.format(search_items_queue.qsize(), db_updates_queue.qsize(), wh_queue.qsize()))

            # Print status of overseer
            status_text.append('{} Overseer: {}'.format(threadStatus['Overseer']['method'], threadStatus['Overseer']['message']))

            # Calculate the total number of pages.  Subtracting 1 for the overseer.
            total_pages = math.ceil((len(threadStatus) - 1) / float(usable_height))

            # Prevent moving outside the valid range of pages
            if current_page[0] > total_pages:
                current_page[0] = total_pages
            if current_page[0] < 1:
                current_page[0] = 1

            # Calculate which lines to print
            start_line = usable_height * (current_page[0] - 1)
            end_line = start_line + usable_height
            current_line = 1

            # Print the worker status
            for item in sorted(threadStatus):
                if(threadStatus[item]['type'] == "Worker"):
                    current_line += 1

                    # Skip over items that don't belong on this page
                    if current_line < start_line:
                        continue
                    if current_line > end_line:
                        break

                    if 'skip' in threadStatus[item]:
                        status_text.append('{} - Success: {}, Failed: {}, No Items: {}, Skipped: {} - {}'.format(item, threadStatus[item]['success'], threadStatus[item]['fail'], threadStatus[item]['noitems'], threadStatus[item]['skip'], threadStatus[item]['message']))
                    else:
                        status_text.append('{} - Success: {}, Failed: {}, No Items: {} - {}'.format(item, threadStatus[item]['success'], threadStatus[item]['fail'], threadStatus[item]['noitems'], threadStatus[item]['message']))
            status_text.append('Page {}/{}.  Type page number and <ENTER> to switch pages.  Press <ENTER> alone to switch between status and log view'.format(current_page[0], total_pages))
            # Clear the screen
            os.system('cls' if os.name == 'nt' else 'clear')
            # Print status
            print "\n".join(status_text)
        time.sleep(1)


# The main search loop that keeps an eye on the over all process
def search_overseer_thread(args, method, new_location_queue, pause_bit, encryption_lib_path, db_updates_queue, wh_queue):

    log.info('Search overseer starting')

    search_items_queue = Queue()
    threadStatus = {}

    threadStatus['Overseer'] = {
        'message': 'Initializing',
        'type': 'Overseer',
        'method': 'Hex Grid' if method == 'hex' else 'Spawn Point'
    }

    if(args.print_status):
        log.info('Starting status printer thread')
        t = Thread(target=status_printer,
                   name='status_printer',
                   args=(threadStatus, search_items_queue, db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    # Create a search_worker_thread per account
    log.info('Starting search worker threads')
    for i, account in enumerate(args.accounts):
        log.debug('Starting search worker thread %d for user %s', i, account['username'])
        workerId = 'Worker {:03}'.format(i)
        threadStatus[workerId] = {
            'type': 'Worker',
            'message': 'Creating thread...',
            'success': 0,
            'fail': 0,
            'noitems': 0
        }

        t = Thread(target=search_worker_thread,
                   name='search-worker-{}'.format(i),
                   args=(args, account, search_items_queue,
                         encryption_lib_path, threadStatus[workerId],
                         db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    '''
    For hex scanning, we can generate the full list of scan points well
    in advance. When then can queue them all up to be searched as fast
    as the threads will allow.

    With spawn point scanning (sps) we can come up with the order early
    on, and we can populate the entire queue, but the individual threads
    will need to wait until the point is available (and ensure it is not
    to late as well).
    '''

    # A place to track the current location
    current_location = False

    # The real work starts here but will halt on pause_bit.set()
    while True:

        # paused; clear queue if needed, otherwise sleep and loop
        while pause_bit.is_set():
            if not search_items_queue.empty():
                try:
                    while True:
                        search_items_queue.get_nowait()
                except Empty:
                    pass
            threadStatus['Overseer']['message'] = "Scanning is paused"
            time.sleep(1)

        # If a new location has been passed to us, get the most recent one
        if not new_location_queue.empty():
            log.info('New location caught, moving search grid')
            try:
                while True:
                    current_location = new_location_queue.get_nowait()
            except Empty:
                pass

            # We (may) need to clear the search_items_queue
            if not search_items_queue.empty():
                try:
                    while True:
                        search_items_queue.get_nowait()
                except Empty:
                    pass

        # If there are no search_items_queue either the loop has finished (or been
        # cleared above) -- either way, time to fill it back up
        if search_items_queue.empty():
            log.debug('Search queue empty, restarting loop')

            # locations = [(lat,lng,not_before,not_after),...]
            if method == 'hex':
                locations = get_hex_location_list(args, current_location)
            else:
                locations = get_sps_location_list(args, current_location)

            if len(locations) == 0:
                log.warning('Nothing to scan!')

            threadStatus['Overseer']['message'] = "Queuing steps"
            for step, step_location in enumerate(locations, 1):
                log.debug('Queueing step %d @ %f/%f/%f', step, step_location[0], step_location[1], step_location[2])
                search_args = (step, step_location[0], step_location[1], step_location[2], step_location[3], step_location[4])
                search_items_queue.put(search_args)
        else:
            #   log.info('Search queue processing, %d items left', search_items_queue.qsize())
            threadStatus['Overseer']['message'] = "Processing search queue"

        # Now we just give a little pause here
        time.sleep(1)


def get_hex_location_list(args, current_location):
    # if we are only scanning for pokestops/gyms, then increase step radius to visibility range
    if args.no_pokemon:
        step_distance = 0.900
    else:
        step_distance = 0.070

    # update our list of coords
    locations = list(generate_location_steps(current_location, args.step_limit, step_distance))

    # In hex "spawns only" mode, filter out scan locations with no history of pokemons
    if args.spawnpoints_only and not args.no_pokemon:
        n, e, s, w = Pokemon.hex_bounds(current_location, args.step_limit)
        spawnpoints = set((d['latitude'], d['longitude']) for d in Pokemon.get_spawnpoints(s, w, n, e))

        if len(spawnpoints) == 0:
            log.warning('No spawnpoints found in the specified area! (Did you forget to run a normal scan in this area first?)')

        def any_spawnpoints_in_range(coords):
            return any(geopy.distance.distance(coords, x).meters <= 70 for x in spawnpoints)

        locations = [coords for coords in locations if any_spawnpoints_in_range(coords)]

    # put into the right struture with zero'ed before/after values
    locationsZeroed = []
    for location in locations:
        locationsZeroed.append((location[0], location[1], 0, 0, 0))

    return locationsZeroed


def get_sps_location_list(args, current_location):
    # Attempt to load spawns from file; otherwise use the database records
    if os.path.isfile(args.spawnpoint_scanning):
        log.debug('Loading spawn points from json file @ %s', args.spawnpoint_scanning)
        locations = []
        try:
            with open(args.spawnpoint_scanning) as file:
                locations = json.load(file)
        except IOError as e:
            log.error('Error opening json file: %s', e)
        except ValueError as e:
            log.error('JSON error: %s', e)

    if not len(locations):
        log.debug('Loading spawn points from database')
        locations = Pokemon.get_spawnpoints_in_hex(current_location, args.step_limit)

    if not len(locations):
        raise Exception('No availabe spawn points!')

    # Put the spawn points in order by spawn time
    locations.sort(key=itemgetter('time'))

    log.info('Total of %d spawns to track', len(locations))

    # Find the next location to scan by time
    pos = SbSearch(locations, (curSec() + 3540) % 3600)

    # Then reslice the list to get the scan order right
    locations = locations[pos:] + locations[:pos]

    # todo: set location = [ (loc, notbefore, notafter), ...]
    return locations

    # while True:
    #     threadStatus['Overseer']['message'] = "Waiting for spawnpoints {} of {} to spawn at {}".format(pos, len(spawns), spawns[pos]['time'])
    #     while timeDif(curSec(), spawns[pos]['time']) < 60:
    #         time.sleep(1)
    #     # make location with a dummy height (seems to be more reliable than 0 height)
    #     threadStatus['Overseer']['message'] = "Queuing spawnpoint {} of {}".format(pos, len(spawns))
    #     location = [spawns[pos]['lat'], spawns[pos]['lng'], 40.32]
    #     search_args = (pos, location, spawns[pos]['time'])
    #     search_items_queue.put(search_args)
    #     pos = (pos + 1) % len(spawns)


# def search_overseer_thread_ss(args, new_location_queue, pause_bit, encryption_lib_path, db_updates_queue, wh_queue):
#     log.info('Search ss overseer starting')
#     search_items_queue = Queue()
#     spawns = []
#     threadStatus = {}

#     threadStatus['Overseer'] = {'message': 'Initializing', 'type': 'Overseer', 'method': 'Spawn Scan'}

#     if(args.print_status):
#         log.info('Starting status printer thread')
#         t = Thread(target=status_printer,
#                   name='status_printer',
#                   args=(threadStatus, search_items_queue, db_updates_queue, wh_queue))
#         t.daemon = True
#         t.start()

#     # Create a search_worker_thread_ss per account
#     log.info('Starting search worker threads')
#     for i, account in enumerate(args.accounts):
#         log.debug('Starting search worker thread %d for user %s', i, account['username'])
#         threadStatus['Worker {:03}'.format(i)] = {}
#         threadStatus['Worker {:03}'.format(i)]['type'] = "Worker"
#         threadStatus['Worker {:03}'.format(i)]['message'] = "Creating thread..."
#         threadStatus['Worker {:03}'.format(i)]['success'] = 0
#         threadStatus['Worker {:03}'.format(i)]['fail'] = 0
#         threadStatus['Worker {:03}'.format(i)]['skip'] = 0
#         threadStatus['Worker {:03}'.format(i)]['noitems'] = 0
#         t = Thread(target=search_worker_thread_ss,
#                   name='ss-worker-{}'.format(i),
#                   args=(args, account, search_items_queue,
#                          encryption_lib_path, threadStatus['Worker {:03}'.format(i)],
#                          db_updates_queue, wh_queue))
#         t.daemon = True
#         t.start()
#
#     if os.path.isfile(args.spawnpoint_scanning):  # if the spawns file exists use it
#         threadStatus['Overseer']['message'] = "Getting spawnpoints from file"
#         try:
#             with open(args.spawnpoint_scanning) as file:
#                 try:
#                     spawns = json.load(file)
#                 except ValueError:
#                     log.error(args.spawnpoint_scanning + " is not valid")
#                     return
#                 file.close()
#         except IOError:
#             log.error("Error opening " + args.spawnpoint_scanning)
#             return
#     else:  # if spawns file dose not exist use the db
#         threadStatus['Overseer']['message'] = "Getting spawnpoints from database"
#         loc = new_location_queue.get()
#         spawns = Pokemon.get_spawnpoints_in_hex(loc, args.step_limit)
#     spawns.sort(key=itemgetter('time'))
#     log.info('Total of %d spawns to track', len(spawns))
#     # find the inital location (spawn thats 60sec old)
#     pos = SbSearch(spawns, (curSec() + 3540) % 3600)
#     while True:
#         threadStatus['Overseer']['message'] = "Waiting for spawnpoints {} of {} to spawn at {}".format(pos, len(spawns), spawns[pos]['time'])
#         while timeDif(curSec(), spawns[pos]['time']) < 60:
#             time.sleep(1)
#         # make location with a dummy height (seems to be more reliable than 0 height)
#         threadStatus['Overseer']['message'] = "Queuing spawnpoint {} of {}".format(pos, len(spawns))
#         location = [spawns[pos]['lat'], spawns[pos]['lng'], 40.32]
#         search_args = (pos, location, spawns[pos]['time'])
#         search_items_queue.put(search_args)
#         pos = (pos + 1) % len(spawns)


def search_worker_thread(args, account, search_items_queue, encryption_lib_path, status, dbq, whq):

    stagger_thread(args, account)

    log.debug('Search worker thread starting')

    # The forever loop for the thread
    while True:
        try:
            # New lease of life right here
            status['fail'] = 0

            # Create the API instance this will use
            if args.mock != '':
                api = FakePogoApi(args.mock)
            else:
                api = PGoApi()

            if args.proxy:
                api.set_proxy({'http': args.proxy, 'https': args.proxy})

            api.activate_signature(encryption_lib_path)

            # The forever loop for the searches
            while True:

                # If this account has been messing up too hard, let it rest
                if status['fail'] > args.max_failures:
                    end_sleep = time.time() + (3600 * 2)
                    long_sleep_started = time.strftime("%H:%M")
                    while time.time() < end_sleep:
                        status['message'] = 'Worker {} failed more than {} scans; possibly banned account. Sleeping for 2 hour sleep as of {}'.format(account['username'], max_failures, long_sleep_started)
                        log.error(status['message'])
                        time.sleep(300)
                    break  # exit this loop to have the API recreated

                # Grab the next thing to search (when available)
                status['message'] = "Waiting for item from queue"
                step, step_location, not_before, not_after = search_items_queue.get()

                # too soon?
                if not_before and not_before < time.time():
                    status['message'] = 'Worker {} is early for location {},{}; waiting for the right time'.format(account['username'], step_location[0], step_location[1])
                    log.info(status['message'])
                    while not_before < time.time():
                        time.sleep(1)

                # too late?
                if not_after and not_after > time.time():
                    search_items_queue.task_done()
                    status['skip'] += 1
                    status['message'] = 'Worker {} was too late for location {},{}; skipping'.format(account['username'], step_location[0], step_location[1])
                    log.info(status['message'])
                    time.sleep(args.scan_delay)
                    continue

                status['message'] = "Searching at {},{}".format(step_location[0], step_location[1])
                log.info('Search step %d beginning (queue size is %d)', step, search_items_queue.qsize())

                # Let the api know where we intend to be for this loop
                api.set_position(*step_location)

                # Ok, let's get started -- check our login status
                check_login(args, account, api, step_location)

                # Make the actual request (finally!)
                response_dict = map_request(api, step_location, args.jitter)

                # G'damnit, nothing back. Mark it up, sleep, carry on
                if not response_dict:
                    status['fail'] += 1
                    status['message'] = "Invalid response at {},{}, abandoning location. Username: {}".format(failed_total, step_location[0], step_location[1], sleep_time, account['username'])
                    log.error(status['message'])
                    time.sleep(args.scan_delay)
                    continue

                # Got the response, parse it out, send todo's to db/wh queues
                try:
                    findCount = parse_map(args, response_dict, step_location, dbq, whq)
                    search_items_queue.task_done()
                    status[('success' if findCount > 0 else 'noitems')] += 1
                    status['message'] = "Search at {},{} completed with {} finds. Username: {}".format(step_location[0], step_location[1], findCount, account['username'])
                    log.debug(status['message'])
                except KeyError:
                    status['fail'] += 1
                    status['message'] = "Map parse failed at {},{}, abandoning location. Username: {}".format(step_location[0], step_location[1], account['username'])
                    log.exception(status['message'])

                # Always delay the desired amount after "scan" completion
                time.sleep(args.scan_delay)

        # catch any process exceptions, log them, and continue the thread
        except Exception as e:
            status['message'] = "Exception in search_worker: %s. Username: {}".format(e, account['username'])
            log.exception(status['message'])
            time.sleep(args.scan_delay)


def check_login(args, account, api, position):

    # Logged in? Enough time left? Cool!
    if api._auth_provider and api._auth_provider._ticket_expire:
        remaining_time = api._auth_provider._ticket_expire / 1000 - time.time()
        if remaining_time > 60:
            log.debug('Credentials remain valid for another %f seconds', remaining_time)
            return

    # Try to login (a few times, but don't get stuck here)
    i = 0
    api.set_position(position[0], position[1], position[2])
    while i < args.login_retries:
        try:
            if args.proxy:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'], proxy_config={'http': args.proxy, 'https': args.proxy})
            else:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'])
            break
        except AuthException:
            if i >= args.login_retries:
                raise TooManyLoginAttempts('Exceeded login attempts')
            else:
                i += 1
                log.error('Failed to login to Pokemon Go with account %s. Trying again in %g seconds', account['username'], args.login_delay)
                time.sleep(args.login_delay)

    log.debug('Login for account %s successful', account['username'])


def map_request(api, position, jitter=False):
    # create scan_location to send to the api based off of position, because tuples aren't mutable
    if jitter:
        # jitter it, just a little bit.
        scan_location = jitterLocation(position)
        log.debug("Jittered to: %f/%f/%f", scan_location[0], scan_location[1], scan_location[2])
    else:
        # Just use the original coordinates
        scan_location = position

    try:
        cell_ids = util.get_cell_ids(scan_location[0], scan_location[1])
        timestamps = [0, ] * len(cell_ids)
        return api.get_map_objects(latitude=f2i(scan_location[0]),
                                   longitude=f2i(scan_location[1]),
                                   since_timestamp_ms=timestamps,
                                   cell_id=cell_ids)
    except Exception as e:
        log.warning('Exception while downloading map: %s', e)
        return False


# Delay each thread start time so that logins only occur ~1s
def stagger_thread(args, account):
    if args.accounts.index(account) == 0:
        return  # No need to delay the first one
    delay = args.accounts.index(account) + ((random.random() - .5) / 2)
    log.debug('Delaying thread startup for %.2f seconds', delay)
    time.sleep(delay)


class TooManyLoginAttempts(Exception):
    pass
