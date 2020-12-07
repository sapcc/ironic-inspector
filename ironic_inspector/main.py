# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import os
import random
import re

import flask
from oslo_utils import strutils
from oslo_utils import uuidutils
from werkzeug.middleware import proxy_fix

from ironic_inspector import api_tools
from ironic_inspector.common import context
from ironic_inspector.common import coordination
from ironic_inspector.common.i18n import _
from ironic_inspector.common import ironic as ir_utils
from ironic_inspector.common import rpc
from ironic_inspector.conductor import manager
import ironic_inspector.conf
from ironic_inspector.conf import opts as conf_opts
from ironic_inspector import node_cache
from ironic_inspector import process
from ironic_inspector import rules
from ironic_inspector import utils

CONF = ironic_inspector.conf.CONF


_app = flask.Flask(__name__)
_wsgi_app = _app.wsgi_app

LOG = utils.getProcessingLogger(__name__)

MINIMUM_API_VERSION = (1, 0)
CURRENT_API_VERSION = (1, 17)
DEFAULT_API_VERSION = CURRENT_API_VERSION
_LOGGING_EXCLUDED_KEYS = ('logs',)


def _init_middleware():
    """Initialize WSGI middleware.

    :returns: None
    """

    # Ensure original root app is restored and wrap it with ProxyFix,
    # respecting only the last entry in each header if it contains a list of
    # values. The following headers are respected: X-Forwarded-For,
    # X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Port,
    # X-Forwarded-Prefix (the last one sets SCRIPT_NAME environment variable
    # that is used to construct links).
    _app.wsgi_app = proxy_fix.ProxyFix(
        _wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    if CONF.auth_strategy == 'keystone':
        utils.add_auth_middleware(_app)
    elif CONF.auth_strategy == 'http_basic':
        utils.add_basic_auth_middleware(_app)
    else:
        LOG.warning('Starting unauthenticated, please check'
                    ' configuration')
    utils.add_cors_middleware(_app)


def get_app():
    """Get the flask instance."""
    _init_middleware()
    return _app


# TODO(kaifeng) Extract rpc related code into a rpcapi module
def get_random_topic():
    coordinator = coordination.get_coordinator(prefix='api')
    members = coordinator.get_members()
    hosts = []
    for member in members:
        # NOTE(kaifeng) recomposite host in case it contains '.'
        parts = member.decode('ascii').split('.')
        if len(parts) < 3:
            LOG.warning('Found invalid member %s', member)
            continue

        if parts[1] == 'conductor':
            hosts.append('.'.join(parts[2:]))

    if not hosts:
        raise utils.NoAvailableConductor('No available conductor service')

    topic = '%s.%s' % (manager.MANAGER_TOPIC, random.choice(hosts))
    return topic


def get_client_compat():
    if CONF.standalone:
        return rpc.get_client()

    topic = get_random_topic()
    return rpc.get_client(topic)


def _get_version():
    ver = flask.request.headers.get(conf_opts.VERSION_HEADER,
                                    _DEFAULT_API_VERSION)
    try:
        if ver.lower() == 'latest':
            requested = CURRENT_API_VERSION
        else:
            requested = tuple(int(x) for x in ver.split('.'))
    except (ValueError, TypeError):
        return error_response(_('Malformed API version: expected string '
                                'in form of X.Y or latest'), code=400)
    return requested


def _format_version(ver):
    return '%d.%d' % ver


_DEFAULT_API_VERSION = _format_version(DEFAULT_API_VERSION)


def error_response(exc, code=500):
    res = flask.jsonify(error={'message': str(exc)})
    res.status_code = code
    LOG.debug('Returning error to client: %s', exc)
    return res


def _generate_empty_response(code):
    """Change the content mime type to text/plain.

    :param code: The HTTP response code as an integer.
    :returns: An empty flask response object with the
              requested return code.
    """
    # NOTE(TheJulia): Explicitly set a mime type and body on the
    # response as some proxies view the lack of a mime type as a
    # failure when the request was actually successful.
    # Strictly speaking, 204s should have no body, where as 202's
    # don't strictly require or expect content, but content can
    # be included for user friendly response bodies.
    if code == 204:
        response = flask.make_response('', code)
        response.mimetype = 'text/plain'
    else:
        # Send an empty dictionary to set a mimetype, and ultimately
        # with this being a rest API we can, at some point, choose to
        # convey some sort of status response back in the message
        # body.
        response = flask.make_response({}, code)
    return response


def convert_exceptions(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except utils.Error as exc:
            return error_response(exc, exc.http_code)
        except Exception as exc:
            LOG.exception('Internal server error')
            msg = _('Internal server error')
            if CONF.debug:
                msg += ' (%s): %s' % (exc.__class__.__name__, exc)
            return error_response(msg)

    return wrapper


@_app.before_first_request
def start_coordinator():
    """Create a coordinator instance for non-standalone case."""
    if not CONF.standalone:
        coordinator = coordination.get_coordinator(prefix='api')
        coordinator.start(heartbeat=False)
        LOG.info('Sucessfully created coordinator.')


@_app.before_request
def check_api_version():
    requested = _get_version()

    if requested < MINIMUM_API_VERSION or requested > CURRENT_API_VERSION:
        return error_response(_('Unsupported API version %(requested)s, '
                                'supported range is %(min)s to %(max)s') %
                              {'requested': _format_version(requested),
                               'min': _format_version(MINIMUM_API_VERSION),
                               'max': _format_version(CURRENT_API_VERSION)},
                              code=406)


@_app.after_request
def add_version_headers(res):
    res.headers[conf_opts.MIN_VERSION_HEADER] = '%s.%s' % MINIMUM_API_VERSION
    res.headers[conf_opts.MAX_VERSION_HEADER] = '%s.%s' % CURRENT_API_VERSION
    return res


def create_link_object(urls):
    links = []
    for url in urls:
        links.append({
            "rel": "self",
            "href": os.path.join(
                os.path.join(flask.request.url_root,
                             os.environ.get('SCRIPT_NAME', '/').lstrip('/')),
                url).rstrip('/')})
    return links


def generate_resource_data(resources):
    data = []
    for resource in resources:
        item = {}
        item['name'] = str(resource).rstrip('/').split('/')[-1]
        item['links'] = create_link_object([str(resource)[1:]])
        data.append(item)
    return data


def generate_introspection_status(node):
    """Return a dict representing current node status.

    :param node: a NodeInfo instance
    :return: dictionary
    """
    started_at = node.started_at.isoformat()
    finished_at = node.finished_at.isoformat() if node.finished_at else None

    status = {}
    status['uuid'] = node.uuid
    status['finished'] = bool(node.finished_at)
    status['state'] = node.state
    status['started_at'] = started_at
    status['finished_at'] = finished_at
    status['error'] = node.error
    status['links'] = create_link_object(
        ["v%s/introspection/%s" % (CURRENT_API_VERSION[0], node.uuid)])
    return status


def api(path, is_public_api=False, rule=None, verb_to_rule_map=None,
        **flask_kwargs):
    """Decorator to wrap api methods.

    Performs flask routing, exception conversion,
    generation of oslo context for request and API access policy enforcement.

    :param path: flask app route path
    :param is_public_api: whether this API path should be treated
                          as public, with minimal access enforcement
    :param rule: API access policy rule to enforce.
                 If rule is None, the 'default' policy rule will be enforced,
                 which is "deny all" if not overridden in policy confif file.
    :param verb_to_rule_map: if both rule and this are given,
                             defines mapping between http verbs (uppercase)
                             and strings to format the 'rule' string with
    :param kwargs: all the rest kwargs are passed to flask app.route
    """
    # Force uniform behavior with regards to trailing slashes
    if not path.endswith('/'):
        path = path + '/'
    flask_kwargs['strict_slashes'] = False

    def outer(func):
        @_app.route(path, **flask_kwargs)
        @convert_exceptions
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            flask.request.context = context.RequestContext.from_environ(
                flask.request.environ,
                is_public_api=is_public_api)
            if verb_to_rule_map and rule:
                policy_rule = rule.format(
                    verb_to_rule_map[flask.request.method.upper()])
            else:
                policy_rule = rule
            utils.check_auth(flask.request, rule=policy_rule)
            return func(*args, **kwargs)
        return wrapper
    return outer


@api('/', rule='introspection', is_public_api=True, methods=['GET'])
def api_root():
    versions = [
        {
            "status": "CURRENT",
            "id": '%s.%s' % CURRENT_API_VERSION,
        },
    ]

    for version in versions:
        version['links'] = create_link_object(
            ["v%s" % version['id'].split('.')[0]])

    return flask.jsonify(versions=versions)


@api('/<version>', rule='introspection:version', is_public_api=True,
     methods=['GET'])
def version_root(version):
    pat = re.compile(r'^\/%s\/[^\/]*?/?$' % version)

    resources = []
    for url in _app.url_map.iter_rules():
        if pat.match(str(url)):
            resources.append(url)

    if not resources:
        raise utils.Error(_('Version %s not found.') % version, code=404)

    return flask.jsonify(resources=generate_resource_data(resources))


@api('/v1/continue', rule="introspection:continue", is_public_api=True,
     methods=['POST'])
def api_continue():
    data = flask.request.get_json(force=True)
    if not isinstance(data, dict):
        raise utils.Error(_('Invalid data: expected a JSON object, got %s') %
                          data.__class__.__name__)

    logged_data = {k: (v if k not in _LOGGING_EXCLUDED_KEYS else '<hidden>')
                   for k, v in data.items()}
    LOG.debug("Received data from the ramdisk: %s", logged_data,
              data=data)

    client = get_client_compat()
    result = client.call({}, 'do_continue', data=data)
    return flask.jsonify(result)


# TODO(sambetts) Add API discovery for this endpoint
@api('/v1/introspection/<node_id>',
     rule="introspection:{}",
     verb_to_rule_map={'GET': 'status', 'POST': 'start'},
     methods=['GET', 'POST'])
def api_introspection(node_id):
    if flask.request.method == 'POST':
        args = flask.request.args

        manage_boot = args.get('manage_boot', 'True')
        try:
            manage_boot = strutils.bool_from_string(manage_boot, strict=True)
        except ValueError:
            raise utils.Error(_('Invalid boolean value for manage_boot: %s') %
                              manage_boot, code=400)

        if manage_boot and not CONF.can_manage_boot:
            raise utils.Error(_('Managed boot is requested, but this '
                                'installation cannot manage boot ('
                                '(can_manage_boot set to False)'),
                              code=400)
        client = get_client_compat()
        client.call({}, 'do_introspection', node_id=node_id,
                    manage_boot=manage_boot,
                    token=flask.request.headers.get('X-Auth-Token'))
        return _generate_empty_response(202)
    else:
        node_info = node_cache.get_node(node_id)
        return flask.json.jsonify(generate_introspection_status(node_info))


@api('/v1/introspection', rule='introspection:status', methods=['GET'])
def api_introspection_statuses():
    nodes = node_cache.get_node_list(
        marker=api_tools.marker_field(),
        limit=api_tools.limit_field(default=CONF.api_max_limit)
    )
    data = {
        'introspection': [generate_introspection_status(node)
                          for node in nodes]
    }
    return flask.json.jsonify(data)


@api('/v1/introspection/<node_id>/abort', rule="introspection:abort",
     methods=['POST'])
def api_introspection_abort(node_id):
    client = get_client_compat()
    client.call({}, 'do_abort', node_id=node_id,
                token=flask.request.headers.get('X-Auth-Token'))
    return _generate_empty_response(202)


def _get_data(node_id, processed):
    try:
        if not uuidutils.is_uuid_like(node_id):
            node = ir_utils.get_node(node_id, fields=['uuid'])
            node_id = node.id
        res = process.get_introspection_data(node_id, processed=processed)
        return res, 200, {'Content-Type': 'application/json'}
    except utils.IntrospectionDataStoreDisabled:
        return error_response(_('Inspector is not configured to store data. '
                                'Set the [processing]store_data '
                                'configuration option to change this.'),
                              code=404)


@api('/v1/introspection/<node_id>/data', rule="introspection:data",
     methods=['GET'])
def api_introspection_data(node_id):
    return _get_data(node_id, True)


@api('/v1/introspection/<node_id>/data/unprocessed', rule="introspection:data",
     methods=['GET'])
def api_introspection_unprocessed_data(node_id):
    return _get_data(node_id, False)


@api('/v1/introspection/<node_id>/data/unprocessed',
     rule="introspection:reapply", methods=['POST'])
def api_introspection_reapply(node_id):
    data = None
    if flask.request.content_length:
        try:
            data = flask.request.get_json(force=True)
        except Exception:
            raise utils.Error(
                _('Invalid data: expected a JSON object, got %s') % data)
        if not isinstance(data, dict):
            raise utils.Error(
                _('Invalid data: expected a JSON object, got %s') %
                data.__class__.__name__)
        LOG.debug("Received reapply data from request", data=data)

    if not uuidutils.is_uuid_like(node_id):
        node = ir_utils.get_node(node_id, fields=['uuid'])
        node_id = node.id

    client = get_client_compat()
    client.call({}, 'do_reapply', node_uuid=node_id, data=data)
    return _generate_empty_response(202)


def rule_repr(rule, short):
    result = rule.as_dict(short=short)
    result['links'] = [{
        'href': flask.url_for('api_rule', uuid=result['uuid']).rstrip('/'),
        'rel': 'self'
    }]
    return result


@api('/v1/rules',
     rule="introspection:rule:{}",
     verb_to_rule_map={'GET': 'get', 'POST': 'create', 'DELETE': 'delete'},
     methods=['GET', 'POST', 'DELETE'])
def api_rules():
    if flask.request.method == 'GET':
        res = [rule_repr(rule, short=True) for rule in rules.get_all()]
        return flask.jsonify(rules=res)
    elif flask.request.method == 'DELETE':
        rules.delete_all()
        return _generate_empty_response(204)
    else:
        body = flask.request.get_json(force=True)
        if body.get('uuid') and not uuidutils.is_uuid_like(body['uuid']):
            raise utils.Error(_('Invalid UUID value'), code=400)
        if body.get('scope') and len(body.get('scope')) > 255:
            raise utils.Error(
                _("Invalid scope: the length of the scope should be within "
                  "255 characters"), code=400)

        rule = rules.create(conditions_json=body.get('conditions', []),
                            actions_json=body.get('actions', []),
                            uuid=body.get('uuid'),
                            description=body.get('description'),
                            scope=body.get('scope'))

        response_code = (200 if _get_version() < (1, 6) else 201)
        return flask.make_response(
            flask.jsonify(rule_repr(rule, short=False)), response_code)


@api('/v1/rules/<uuid>',
     rule="introspection:rule:{}",
     verb_to_rule_map={'GET': 'get', 'DELETE': 'delete'},
     methods=['GET', 'DELETE'])
def api_rule(uuid):
    if flask.request.method == 'GET':
        rule = rules.get(uuid)
        return flask.jsonify(rule_repr(rule, short=False))
    else:
        rules.delete(uuid)
        return _generate_empty_response(204)


@_app.errorhandler(404)
def handle_404(error):
    return error_response(error, code=404)
