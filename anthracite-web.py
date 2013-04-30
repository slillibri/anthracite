#!/usr/bin/env python2
from bottle import route, run, debug, template, request, static_file, error, response, app, hook
from backend import Backend, Event, Reportpoint, load_plugins
from beaker.middleware import SessionMiddleware
import json
import os
import time
import sys
from view import page
from collections import deque

session = None


@hook('before_request')
def track_history():
    '''
    maintain a list of the 10 most recent pages loaded per earch particular user
    '''
    global session
    # ignore everything that's not a page being loaded by the user:
    if request.fullpath.startswith('/assets'):
        return
    # loaded in background by report page
    if request.fullpath.startswith('/report/data'):
        return
    session = request.environ.get('beaker.session')
    session['history'] = session.get('history', deque())
    # loaded in background by timeline
    if len(session['history']) and request.fullpath == '/events/xml' and session['history'][len(session['history']) - 1] == '/events/timeline':
        return
    # note the url always misses the '#foo' part
    url = request.fullpath
    if request.query_string:
        url += "?%s" % request.query_string
    if len(session['history']) and url == session['history'][len(session['history']) - 1]:
        return
    session['history'].append(url)
    if len(session['history']) > 10:
        session['history'].popleft()
    session.save()


@route('/')
def main():
    return p(body=template('tpl/index'), page='main')


@route('/events/table')
def events_table():
    return p(body=template('tpl/events_table', events=backend.get_events_objects()), page='table')


@route('/events/timeline')
def events_timeline():
    (range_low, range_high) = backend.get_events_range()

    return p(body=template('tpl/events_timeline', range_low=range_low, range_high=range_high), page='timeline')


@route('/events/json')
def events_json():
    '''
    much like http://localhost:9200/anthracite/event/_search?q=*:*&pretty=true
    but: displays only the actual events, not index etc, they are sorted, and uses unix timestamps
    '''
    return {"events": backend.get_events_raw()}


@route('/events/csv')
def events_csv():
    '''
    returns the first line of every event
    '''
    response.content_type = 'text/plain'
    events = []
    for event in backend.get_events_raw():
        formatted = [event['id'], str(event['date']), event['desc'][:event['desc'].find('\n')], ' '.join(event['tags'])]
        events.append(','.join(formatted))
    return "\n".join(events)


@route('/events/jsonp')
def events_jsonp():
    response.content_type = 'application/x-javascript'
    jsonp = request.query.jsonp or 'jsonp'
    return '%s(%s);' % (jsonp, json.dumps(events_json()))


@route('/events/xml')
def events_xml():
    response.content_type = 'application/xml'
    return template('tpl/events_xml', events=backend.get_events_raw())


@route('/events/delete/<event_id>')
def events_delete(event_id):
    try:
        backend.delete_event(event_id)
        time.sleep(1)
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), successes=['The event was deleted from the database'], page='table')
    except Exception, e:
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), errors=[('Could not delete event', e)], page='table')


@route('/events/edit/<event_id>')
def events_edit(event_id):
    try:
        event = backend.get_event(event_id)
        return p(body=template('tpl/events_edit', event=event, tags=backend.get_tags()), page='edit')
    except Exception, e:
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), errors=[('Could not load event', e)], page='table')


def local_datepick_to_unix_timestamp(datepick):
    '''
    in: something like 12/31/2012 10:25:35 PM, which is local time.
    out: unix timestamp
    '''
    import time
    import datetime
    return int(time.mktime(datetime.datetime.strptime(datepick, "%m/%d/%Y %I:%M:%S %p").timetuple()))


@route('/events/edit/<event_id>', method='POST')
def events_edit_post(event_id):
    try:
        ts = local_datepick_to_unix_timestamp(request.forms.event_datetime)
        # (select2 tags form field uses comma)
        tags = request.forms.event_tags.split(',')
        event = Event(timestamp=ts, desc=request.forms.event_desc, tags=tags, event_id=event_id)
    except Exception, e:
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), errors=[('Could not recreate event from received information', e)], page='table')
    try:
        backend.edit_event(event)
        time.sleep(1)
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), successes=['The event was updated'], page='table')
    except Exception, e:
        return p(body=template('tpl/events_table', events=backend.get_events_objects()), errors=[('Could not update event', e)], page='table')


@route('/events/add', method='GET')
def events_add():
    return p(body=template('tpl/events_add', tags=backend.get_tags(), extra_attributes=config.extra_attributes,
                           helptext=config.helptext, recommended_tags=config.recommended_tags), page='add')


def add_post_handler_default(request, config):
    tags = request.forms.getall('event_tags_recommended')
    # (select2 tags form field uses comma)
    tags.extend(request.forms.event_tags.split(','))
    ts = local_datepick_to_unix_timestamp(request.forms.event_datetime)
    desc = request.forms.event_desc
    extra_attributes = {}
    for attribute in config.extra_attributes:
        if attribute.mandatory:
            if attribute.key not in request.forms:
                raise Exception(attribute.key + " not found in submitted data")
            elif not request.forms[attribute.key]:
                raise Exception(attribute.key + " is empty.  you have to do better")
        # if you want to get pedantic, you can check if the received values match predefined options
        if attribute.key in request.forms and request.forms[attribute.key]:
            extra_attributes[attribute.key] = request.forms[attribute.key]

    # there may be fields we didn't predict (i.e. from scripts that submit
    # events programmatically).  let's just store those as additional
    # attributes.
    # to start, remove all attributes we know of must exist:
    for attrib in ['event_desc', 'event_datetime', 'event_tags']:
        del request.forms[attrib]
    # this one is optional:
    try:
        del request.forms['event_timestamp']
    except KeyError:
        pass
    # some of these keys may not exist.  (i.e. if no field was selected)
    # proper validation was performed above, so we can ignore missing keys
    for attrib in [attribute.key for attribute in config.extra_attributes]:
        try:
            del request.forms[attrib]
        except KeyError:
            pass
    # after all these deletes, only the extra fields remain. get rid of entries with
    # empty values, and store them.
    # (this *should* work for strings and lists...)
    for key in request.forms.keys():
        v = request.forms.getall(key)
        if v:
            extra_attributes[k] = v
    event = Event(timestamp=ts, desc=desc, tags=tags, extra_attributes=extra_attributes)
    return event


@route('/events/add', method='POST')
@route('/events/add/<handler>', method='POST')
def events_add_post(handler='default'):
    try:
        event = globals()['add_post_handler_' + handler](request, config)
    except Exception, e:
        return p(body=template('tpl/events_add', tags=backend.get_tags(), extra_attributes=config.extra_attributes,
                               helptext=config.helptext, recommended_tags=config.recommended_tags),
                 errors=[('Could not create new event', e)], page='add')
    try:
        backend.add_event(event)
        return p(body=template('tpl/events_add', tags=backend.get_tags(), extra_attributes=config.extra_attributes,
                               helptext=config.helptext, recommended_tags=config.recommended_tags),
                 successes=['The new event was added into the database'], page='add')
    except Exception, e:
        return p(body=template('tpl/events_add', tags=backend.get_tags(), extra_attributes=config.extra_attributes,
                               helptext=config.helptext, recommended_tags=config.recommended_tags),
                 errors=[('Could not save new event', e)], page='add')


@route('/events/add/script', method='POST')
def events_add_script():
    try:
        event = Event(timestamp=int(request.forms.event_timestamp),
                      desc=request.forms.event_desc,
                      tags=request.forms.event_tags.split())
    except Exception, e:
        return 'Could not create new event: %s' % e
    try:
        backend.add_event(event)
        return 'The new event was added into the database'
    except Exception, e:
        return 'Could not save new event: %s' % e


@route('/report')
def report():
    import time
    start = local_datepick_to_unix_timestamp(config.opsreport_start)
    return p(page='report', body=template('tpl/report', config=config, reportpoints=get_report_data(start, int(time.time()))))


def get_report_data(start, until):
    events = backend.get_outage_events()
    # see report.tpl for definitions
    # this simple model ignores overlapping outages!
    tttf = 0
    tttd = 0
    tttr = 0
    age = 0  # time spent since start
    last_failure = start
    reportpoints = []
    # TODO there's some assumptions on tag order and such. if your events are
    # badly tagged, things could go wrong.
    # TODO honor start/until
    origin_event = Event(start, "start", [])
    reportpoints.append(Reportpoint(origin_event, 0, 100, 0, tttf, 0, tttd, 0, tttr))
    outages_seen = {}
    for event in events:
        if event.timestamp > until:
            break
        age = float(event.timestamp - start)
        ttd = 0
        ttr = 0
        if 'start' in event.tags:
            outages_seen[event.outage] = {'start': event.timestamp}
            ttf = event.timestamp - last_failure
            tttf += ttf
            last_failure = event.timestamp
        elif 'detected' in event.tags:
            ttd = event.timestamp - outages_seen[event.outage]['start']
            tttd += ttd
            outages_seen[event.outage]['ttd'] = ttd
        elif 'resolved' in event.tags:
            ttd = outages_seen[event.outage]['ttd']
            ttr = event.timestamp - outages_seen[event.outage]['start']
            tttr += ttr
            outages_seen[event.outage]['ttr'] = ttr
        else:
            # the outage changed impact. for now just ignore this, cause we
            # don't do anything with impact yet.
            pass
        muptime = float(age - tttr) * 100 / age
        reportpoints.append(Reportpoint(event, len(outages_seen), muptime, ttf, tttf, ttd, tttd, ttr, tttr))

    age = until - start
    end_event = Event(until, "end", [])
    muptime = float(age - tttr) * 100 / age
    reportpoints.append(Reportpoint(end_event, len(outages_seen), muptime, 0, tttf, 0, tttd, 0, tttr))
    return reportpoints


@route('/report/data/<catchall:re:.*>')
def report_data(catchall):
    response.content_type = 'application/x-javascript'
    start = int(request.query['from'])
    until = int(request.query['until'])
    jsonp = request.query['jsonp']
    reportpoints = get_report_data(start, until)
    data = [
        {
            "target": "ttd",
            "datapoints": [[r.ttd / 60, r.event.timestamp] for r in reportpoints]
        },
        {
            "target": "ttr",
            "datapoints": [[r.ttr / 60, r.event.timestamp] for r in reportpoints]
        }
    ]
    return '%s(%s)' % (jsonp, json.dumps(data))


@route('<path:re:/assets/.*>')
def static(path):
    return static_file(path, root='.')


@error(404)
def error404(code):
    return p(body=template('tpl/error', title='404 page not found', msg='The requested page was not found'))


def p(**kwargs):
    return page(config, backend, state, **kwargs)

app_dir = os.path.dirname(__file__)
if app_dir:
    os.chdir(app_dir)

import config
backend = Backend()
state = {}
(state, errors) = load_plugins(config.plugins)
if errors:
    for e in errors:
        sys.stderr.write(str(e))
    sys.exit(2)
session_opts = {
    'session.type': 'file',
    'session.cookie_expires': 300,
    'session.data_dir': './session_data',
    'session.auto': True
}
app = SessionMiddleware(app(), session_opts)
debug(True)
run(app=app, reloader=True, host=config.listen_host, port=config.listen_port)
