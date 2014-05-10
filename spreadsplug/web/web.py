from __future__ import division

import copy
import itertools
import logging
import logging.handlers
import os
import shutil
import StringIO
import subprocess
import time
import zipfile
from collections import deque

import blinker
import pkg_resources
import requests
import zipstream
from flask import (abort, json, jsonify, request, send_file, render_template,
                   url_for, redirect, make_response, Response)
from jpegtran import JPEGImage
from werkzeug.contrib.cache import SimpleCache

import spreads.plugin as plugin
from spreads.vendor.pathlib import Path
from spreads.workflow import Workflow

import persistence
from spreadsplug.web import app
from discovery import discover_servers
from util import (get_image_url, WorkflowConverter,
                  get_thumbnail, find_stick, scale_image)

logger = logging.getLogger('spreadsplug.web')

signals = blinker.Namespace()
on_download_prepared = signals.signal('download:prepared')
on_download_prepare_progressed = signals.signal('download:prepare-progressed')
on_download_finished = signals.signal('download:finished')

# Event Queue for polling endpoints
event_queue = deque(maxlen=2048)

# Simple dictionary-based cache for expensive calculations
cache = SimpleCache()

# Register custom workflow converter for URL routes
app.url_map.converters['workflow'] = WorkflowConverter


class ApiException(Exception):
    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv


@app.errorhandler(ApiException)
def handle_apiexception(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


# ========= #
#  General  #
# ========= #
@app.route('/')
def index():
    """ Deliver static landing page that launches the client-side app. """
    default_config = cache.get('default-config')
    if default_config is None:
        default_config = app.config['default_config'].flatten()
        cache.set('default-config', default_config)
    templates = cache.get('plugin-templates')
    if templates is None:
        templates = get_plugin_templates()
        cache.set('plugin-templates', templates)
    return render_template(
        "index.html",
        debug=app.config['DEBUG'],
        default_config=default_config,
        plugin_templates=templates
    )


def get_plugin_templates():
    """ Return the names of all globally activated plugins and their
        configuration templates.
    """
    config = app.config['default_config']
    plugins = plugin.get_plugins(*config['plugins'].get())
    scanner_exts = [name for name, cls in plugins.iteritems()
                    if any(issubclass(cls, mixin) for mixin in
                           (plugin.CaptureHooksMixin,
                            plugin.TriggerHooksMixin))]
    processor_exts = [name for name, cls in plugins.iteritems()
                      if any(issubclass(cls, mixin) for mixin in
                             (plugin.ProcessHookMixin,
                              plugin.OutputHookMixin))]
    if app.config['mode'] == 'scanner':
        templates = {section: config.templates[section]
                     for section in config.templates
                     if section in scanner_exts or section == 'device'}
    elif app.config['mode'] == 'processor':
        templates = {section: config.templates[section]
                     for section in config.templates
                     if section in processor_exts}
    elif app.config['mode'] == 'full':
        templates = {section: config.templates[section]
                     for section in config.templates
                     if section != 'core'}
    rv = dict()
    for plugname, options in templates.iteritems():
        if options is None:
            continue
        for key, option in options.iteritems():
            if option.selectable:
                value = [config[plugname][key].get()]
                value += [x for x in option.value if x not in value]
            else:
                value = config[plugname][key].get()
            if not plugname in rv:
                rv[plugname] = dict()
            rv[plugname][key] = dict(value=value,
                                     docstring=option.docstring,
                                     selectable=option.selectable,
                                     advanced=option.advanced)
    return rv


@app.route('/api/plugins')
def get_available_plugins():
    exts = list(pkg_resources.iter_entry_points('spreadsplug.hooks'))
    activated = app.config['default_config']['plugins'].get()
    return jsonify({
        'postprocessing': [ext.name for ext in exts if ext.name in activated
                           and issubclass(ext.load(),
                                          plugin.ProcessHookMixin)],
        'output': [ext.name for ext in exts if ext.name in activated
                   and issubclass(ext.load(), plugin.OutputHookMixin)]
    })


@app.route('/api/plugins/templates')
def template_endpoint():
    return jsonify(get_plugin_templates())

@app.route('/api/remote/discover')
def discover_postprocessors():
    if app.config['mode'] != 'scanner':
        raise ApiException("Discovery only possible when running in 'scanner'"
                           " mode.", 503)
    servers = discover_servers()
    if app.config['postproc_server']:
        servers.append(app.config['postproc_server'].split(':'))
    return jsonify(servers=["{0}:{1}".format(*addr) for addr in servers])


@app.route('/api/remote/plugins')
def get_remote_plugins():
    if app.config['mode'] != 'scanner':
        raise ApiException("Submission only possible when running in 'scanner'"
                           " mode.", 503)
    server = app.config['postproc_server']
    if not server:
        error_msg = ("Remote server was not configured, please set the"
                     "'postprocessing_server' value in your configuration!")
        logger.error(error_msg)
        raise ApiException(error_msg, 500)
    resp = requests.get(server + '/api/plugins')
    return make_response(resp.content, resp.status_code,
                         {'Content-Type': 'application/json'})


@app.route('/api/remote/plugins/templates')
def get_remote_templates():
    if app.config['mode'] != 'scanner':
        raise ApiException("Submission only possible when running in 'scanner'"
                           " mode.", 503)
    server = app.config['postproc_server']
    if not server:
        error_msg = ("Remote server was not configured, please set the"
                     "'postprocessing_server' value in your configuration!")
        logger.error(error_msg)
        raise ApiException(error_msg, 500)
    resp = requests.get(server + '/api/plugins/templates')
    return make_response(resp.content, resp.status_code,
                         {'Content-Type': 'application/json'})


@app.route('/api/log')
def get_logs():
    start = int(request.args.get('start', '0'))
    count = int(request.args.get('count', '50'))
    level = request.args.get('level', 'INFO')
    logbuffer = next(
        x for x in logging.getLogger().handlers
        if isinstance(x, logging.handlers.BufferingHandler)).buffer
    available_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
    if level.upper() not in available_levels:
        levels = available_levels[available_levels.index('INFO'):]
    else:
        levels = available_levels[available_levels.index(level.upper()):]
    msgs = [msg for msg in sorted(logbuffer, key=lambda x: x.relativeCreated,
                                  reverse=True)
            if msg.levelname in levels]
    return jsonify(total_num=len(msgs),
                   messages=msgs[start:start+count])


# ================== #
#  Workflow-related  #
# ================== #
@app.route('/api/workflow', methods=['POST'])
def create_workflow():
    """ Create a new workflow.

    Payload should be a JSON object. The only required attribute is 'name' for
    the desired workflow name. Optionally, 'config' can be set to a
    configuration object in the form "plugin_name: { setting: value, ...}".

    Returns the newly created workflow as a JSON object.
    """
    if request.content_type == 'application/zip':
        zfile = zipfile.ZipFile(StringIO.StringIO(request.data))
        zfile.extractall(path=app.config['base_path'])
        wfname = os.path.dirname(zfile.filelist()[0].filename)
        workflow = Workflow(path=os.path.join(app.config['base_path'], wfname))
    else:
        data = json.loads(request.data)
        path = Path(app.config['base_path'])/unicode(data['name'])

        # Setup default configuration
        config = app.config['default_config']
        # Overlay user-supplied values, if existant
        user_config = data.get('config', None)
        if user_config is not None:
            config = config.with_overlay(user_config)
        workflow = Workflow(config=config, path=path,
                            step=data.get('step', None),
                            step_done=data.get('step_done', None))
    try:
        workflow.id = persistence.save_workflow(workflow)
    except persistence.ValidationError as e:
        return make_response(json.dumps(dict(errors=e.errors)), 400,
                             {'Content-Type': 'application/json'})
    return make_response(json.dumps(workflow),
                         200, {'Content-Type': 'application/json'})


@app.route('/api/workflow', methods=['GET'])
def list_workflows():
    """ Return a list of all workflows. """
    workflows = persistence.get_all_workflows()
    return make_response(json.dumps(workflows.values()),
                         200, {'Content-Type': 'application/json'})


@app.route('/api/workflow/<workflow:workflow>', methods=['GET'])
def get_workflow(workflow):
    """ Return a single workflow. """
    return make_response(json.dumps(workflow),
                         200, {'Content-Type': 'application/json'})


@app.route('/api/workflow/<workflow:workflow>', methods=['PUT'])
def update_workflow(workflow):
    """ Update a single workflow.

    Payload should be a JSON object, as returned by the '/workflow/<id>'
    endpoint.
    Currently the only attribute that can be updated from the client
    is the configuration.

    Returns the updated workflow as a JSON object.
    """
    # TODO: Support renaming a workflow, i.e. rename directory as well
    data = json.loads(request.data)
    name = data.get('name')
    if workflow.path.name != name:
        new_path = workflow.path.parent/name
        workflow.path.rename(new_path)
        workflow.path = new_path
    config = data.get('config')
    # Update workflow configuration
    workflow.config.set(config)
    # Persist to disk
    persistence.save_workflow(workflow)
    return make_response(json.dumps(workflow),
                         200, {'Content-Type': 'application/json'})


@app.route('/api/workflow/<workflow:workflow>', methods=['DELETE'])
def delete_workflow(workflow):
    """ Delete a single workflow from database and disk. """
    # Remove directory
    try:
        shutil.rmtree(unicode(workflow.path))
    except OSError:
        logger.warning("Workflow path {0} could not be removed"
                       .format(workflow.path))
    # Remove from database
    persistence.delete_workflow(workflow.id)
    return jsonify({})


@app.route('/api/events', methods=['GET'])
def get_events():
    """ Get a list of all events that were emitted on the server.

    :param int count:   Number of events to return, default is all in the queue
    :param float since: Only return events that were emitted after the
                        (epoch) timestamp
    """
    count = request.args.get('count', None, int)
    since = request.args.get('since', None, float)
    events = None
    if count:
        events = tuple(itertools.islice(reversed(event_queue), count))[::-1]
    elif since is not None:
        events = tuple(event for event in event_queue
                       if event.emitted > since)
    else:
        events = tuple(event_queue)
    return make_response(
        json.dumps(events),
        200, {'Content-Type': 'application/json'})


@app.route('/api/poll', methods=['GET'])
def poll_for_events():
    """ Wait for events to be emitted on the server.

    If there is a `last_polled` field in the request cookie, it will return
    all events that were emitted since that timestamp. This ensures that no
    events will be missed in a long-polling scenario.
    """
    events = None
    start_time = time.time()
    last_polled = request.cookies.get('last_polled', start_time, float)
    # Only record debug logging events when the app is running in
    # debug mode
    if app.config['DEBUG']:
        skip = lambda event: (
            event.signal.name == 'logrecord'
            and event.data['record'].levelno == logging.DEBUG)
    else:
        skip = lambda event: False
    while time.time() - start_time < 35:
        # NOTE: We need to iterate over a copy of the event queue, since
        #       it might change its content while we iterate
        events = tuple(event for event in copy.copy(event_queue)
                       if event.emitted > last_polled and not skip(event))
        if events:
            resp = make_response(
                json.dumps(events),
                200, {'Content-Type': 'application/json'})
            resp.set_cookie('last_polled', str(events[-1].emitted))
            return resp
        else:
            time.sleep(.1)
    abort(408)  # Request Timeout


@app.route('/api/workflow/<workflow:workflow>/download', methods=['GET'],
           defaults={'fname': None})
@app.route('/api/workflow/<workflow:workflow>/download/<fname>',
           methods=['GET'])
def download_workflow(workflow, fname):
    """ Return a ZIP archive of the current workflow.

    Included all files from the workflow folder as well as the workflow
    configuration as a YAML dump.
    """
    # Set proper file name for zip file
    if fname is None:
        return redirect(url_for('download_workflow', workflow=workflow,
                        fname="{0}.zip".format(workflow.path.stem)))

    # Open ZIP stream
    zstream = zipstream.ZipFile(mode='w', compression=zipstream.ZIP_STORED)
    # Dump configuration to workflow directory
    workflow.config.dump(unicode(workflow.path/'config.yaml'))
    # Find all files within up to two levels deep, relative to the
    # workflow base path
    files = tuple(workflow.path.glob('**/*'))
    num_files = len(files)
    for num, fpath in enumerate(files):
        extract_path = '/'.join((workflow.path.stem,
                                 unicode(fpath.relative_to(workflow.path)))
                                )
        logger.debug("Adding {0} to archive as {1}"
                     .format(fpath, extract_path))
        zstream.write(unicode(fpath), extract_path)
        on_download_prepare_progressed.send(workflow,
                                            progress=(num/num_files),
                                            status=fpath.name)
    zstream_copy = copy.deepcopy(zstream)
    zipsize = sum(len(data) for data in zstream_copy)
    on_download_prepared.send(workflow)

    def zstream_wrapper():
        """ Wrapper around our zstream so we can emit a signal when all data
        has been streamed to the client.
        """
        for data in zstream:
            yield data
        on_download_finished.send()

    response = Response(zstream_wrapper(), mimetype='application/zip')
    response.headers['Content-length'] = int(zipsize)
    return response


@app.route('/api/workflow/<workflow:workflow>/transfer', methods=['POST'])
def transfer_workflow(workflow):
    """ Transfer workflow to an attached USB storage device.

    """
    try:
        stick = find_stick()
    except ImportError:
        return jsonify({"error": "Missing package 'python-dbus', "
                                 "please install."})
    if stick is None:
        return jsonify({"error": "Could not find removable device"}), 503
    from tasks import transfer_to_stick
    transfer_to_stick(workflow.id)
    return 'OK'


@app.route('/api/workflow/<workflow:workflow>/submit', methods=['POST'])
def submit_workflow(workflow):
    """ Submit the requested workflow to the postprocessing server.

    Only available in 'scanner' mode. Requires that the 'postproc_server'
    option is set to the address of a server with the server in 'processor'
    or 'full' mode running.
    """
    if app.config['mode'] != 'scanner':
        raise ApiException("Submission only possible when running in 'scanner'"
                           " mode.", 503)
    server = app.config['postproc_server']
    if not server:
        error_msg = ("Remote server was not configured, please set the"
                     "'postprocessing_server' value in your configuration!")
        logger.error(error_msg)
        raise ApiException(error_msg, 500)
    data = json.loads(request.data)
    user_config = data.get('config', {})
    from tasks import upload_workflow
    # TODO: Pass config to this function
    upload_workflow(workflow.id, server+'/api/workflow', user_config,
                    start_process=data.get('start_process', False),
                    start_output=data.get('start_output', False))
    return 'OK'


# =============== #
#  Image-related  #
# =============== #
@app.route('/api/workflow/<workflow:workflow>/image/<int:img_num>',
           methods=['GET'])
def get_workflow_image(workflow, img_num):
    """ Return image from requested workflow. """
    # Scale image if requested
    width = request.args.get('width', None)
    try:
        img_path = next(p for p in workflow.images
                        if p.stem == "{0:03}".format(img_num))
    except StopIteration:
        abort(404)
    if width:
        return scale_image(unicode(img_path), width=int(width))
    else:
        return send_file(unicode(img_path))


@app.route('/api/workflow/<workflow:workflow>/image/<int:img_num>/thumb',
           methods=['GET'])
def get_workflow_image_thumb(workflow, img_num):
    """ Return thumbnail for image from requested workflow. """
    try:
        img_path = next(p for p in workflow.images
                        if p.stem == "{0:03}".format(img_num))
    except StopIteration:
        abort(404)
    cache_key = "{0}.{1}".format(workflow, img_num)
    thumbnail = None
    if not request.args:
        thumbnail = cache.get(cache_key)
    if thumbnail is None:
        thumbnail = get_thumbnail(img_path)
        cache.set(cache_key, thumbnail)
    return Response(thumbnail, mimetype='image/jpeg')


@app.route('/api/workflow/<workflow:workflow>/image/<int:img_num>',
           methods=['DELETE'])
def delete_workflow_image(workflow, img_num):
    """ Remove a single image from a workflow. """
    try:
        img_path = next(p for p in workflow.images
                        if p.stem == "{0:03}".format(img_num))
    except StopIteration:
        abort(404)
    img_path.unlink()
    return 'OK'


@app.route('/api/workflow/<workflow:workflow>/image/<int:img_num>/crop',
           methods=['POST'])
def crop_workflow_image(workflow, img_num):
    try:
        img_path = next(p for p in workflow.images
                        if p.stem == "{0:03}".format(img_num))
    except StopIteration:
        abort(404)
    img = JPEGImage(unicode(img_path))
    params = {
        'x': int(request.args.get('left', 0)),
        'y': int(request.args.get('top', 0)),
    }
    width = int(request.args.get('width', img.width - params['x']))
    height = int(request.args.get('height', img.height - params['y']))
    if width > img.width:
        width = img.width
    if height > img.height:
        width = img.height
    params['width'] = width
    params['height'] = height
    logger.debug("Cropping \"{0}\" to x:{1} y:{2} w:{3} h:{4}"
                 .format(img_path, *params.values()))
    cropped = img.crop(**params)
    cropped.save(unicode(img_path))
    cache_key = "{0}.{1}".format(workflow, img_num)
    cache.delete(cache_key)
    return 'OK'


# ================= #
#  Capture-related  #
# ================= #
@app.route('/api/workflow/<workflow:workflow>/prepare_capture',
           methods=['POST'])
def prepare_capture(workflow):
    """ Prepare capture for the requested workflow.

    """
    if app.config['mode'] not in ('scanner', 'full'):
        raise ApiException("Only possible when running in 'scanner' or 'full'"
                           " mode.", 503)

    # Check if any other workflow is active and finish, if neccessary
    logger.debug("Finishing previous workflows")
    for wfid, wf in persistence.get_all_workflows().iteritems():
        if wf.active:
            if wfid == workflow.id and not 'reset' in request.args:
                return 'OK'
            wf.finish_capture()
    workflow.prepare_capture()
    return 'OK'


@app.route('/api/workflow/<workflow:workflow>/capture', methods=['POST'])
def trigger_capture(workflow):
    """ Trigger a capture on the requested workflow.

    Optional parameter 'retake' specifies if the last shot is to be retaken.

    Returns the number of pages shot and a list of the images captured by
    this call in JSON notation.
    """
    if app.config['mode'] not in ('scanner', 'full'):
        raise ApiException("Only possible when running in 'scanner' or 'full'"
                           " mode.", 503)
    if workflow.step != 'capture':
        # TODO: Abort with error, since capture has to be prepared first
        workflow.prepare_capture()
    try:
        workflow.capture(retake=('retake' in request.args))
    except IOError as e:
        logger.error(e)
        raise ApiException("Error during capture: {0}".format(e.message), 500)
    return jsonify({
        'pages_shot': len(workflow.images),
        'images': [get_image_url(workflow, x)
                   for x in workflow.images[-2:]]
    })


@app.route('/api/workflow/<workflow:workflow>/finish_capture',
           methods=['POST'])
def finish_capture(workflow):
    """ Wrap up capture process on the requested workflow. """
    if app.config['mode'] not in ('scanner', 'full'):
        raise ApiException("Only possible when running in 'scanner' or 'full'"
                           " mode.", 503)
    workflow.finish_capture()
    return 'OK'


@app.route('/api/workflow/<workflow:workflow>/process', methods=['POST'])
def start_processing(workflow):
    if app.config['mode'] not in ('processor', 'full'):
        raise ApiException("Only possible when running in 'processor' or"
                           " 'full' mode.", 503)
    from tasks import process_workflow
    process_workflow(workflow.id)
    return 'OK'


@app.route('/api/workflow/<workflow:workflow>/output', methods=['POST'])
def start_output_generation(workflow):
    if app.config['mode'] not in ('processor', 'full'):
        raise ApiException("Only possible when running in 'processor' or"
                           " 'full' mode.", 503)
    from tasks import output_workflow
    output_workflow(workflow.id)
    return 'OK'


# ================== #
#   System-related   #
# ================== #
@app.route('/api/system/shutdown', methods=['POST'])
def shutdown():
    if not app.config['standalone']:
        abort(503)
    # NOTE: This requires that the user running spreads can execute
    #       /sbin/shutdown via sudo.
    logger.info("Shutting device down")
    subprocess.call("/usr/bin/sudo /sbin/shutdown -h now".split())
    return ''


@app.route('/<path:path>')
def redirect_pushstate(path):
    return redirect("/#{0}".format(path))
