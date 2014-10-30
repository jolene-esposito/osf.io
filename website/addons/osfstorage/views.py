#!/usr/bin/env python
# encoding: utf-8

import os
import httplib
import logging
import functools

from flask import request

from framework.flask import redirect
from framework.exceptions import HTTPError
from framework.analytics import update_counter

from website.models import User
from website.project.decorators import (
    must_be_valid_project, must_be_contributor, must_be_contributor_or_public,
    must_have_permission, must_not_be_registration, must_have_addon,
)
from website.util import rubeus
from website.project.utils import serialize_node
from website.addons.base.views import check_file_guid

from website.addons.osfstorage import model
from website.addons.osfstorage import utils
from website.addons.osfstorage import errors
from website.addons.osfstorage import settings as osf_storage_settings


logger = logging.getLogger(__name__)


@must_be_contributor
@must_not_be_registration
@must_have_permission('write')
@must_have_addon('osfstorage', 'node')
def osf_storage_request_upload_url(auth, node_addon, **kwargs):
    node = kwargs['node'] or kwargs['project']
    user = auth.user
    path = kwargs.get('path', '')
    try:
        name = request.json['name']
        size = request.json['size']
        content_type = request.json['type']
    except KeyError:
        raise HTTPError(httplib.BAD_REQUEST)
    file_path = os.path.join(path, name)
    return utils.get_upload_url(node, user, size, content_type, file_path)


def make_error(code, reason):
    return HTTPError(
        code,
        data={
            'status': 'error',
            'reason': reason,
        }
    )


def get_payload_from_request(signer, request):
    signature = request.headers.get(osf_storage_settings.SIGNATURE_HEADER_KEY)
    payload = request.get_json()
    if not signer.verify_payload(signature, payload):
        raise make_error(httplib.BAD_REQUEST, 'Invalid signature')
    return payload


def validate_start_hook_payload(payload):
    try:
        user_id = payload['uploadPayload']['extra']['user']
        upload_signature = payload['uploadSignature']
    except KeyError:
        raise HTTPError(httplib.BAD_REQUEST)
    user = User.load(user_id)
    if user is None:
        raise HTTPError(httplib.BAD_REQUEST)
    return user, upload_signature


@must_be_valid_project
@must_have_addon('osfstorage', 'node')
def osf_storage_upload_start_hook(node_addon, **kwargs):
    """
    :raise: `HTTPError` if HMAC signature invalid, path locked, or upload
        signature mismatched
    """
    path = kwargs.get('path', '')
    payload = get_payload_from_request(utils.webhook_signer, request)
    user, upload_signature = validate_start_hook_payload(payload)
    record = model.FileRecord.get_or_create(path, node_addon)
    try:
        record.create_pending_version(user, upload_signature)
    except errors.PathLockedError:
        raise make_error(httplib.CONFLICT, 'File path is locked')
    except errors.SignatureConsumedError:
        raise make_error(httplib.BAD_REQUEST, 'Signature consumed')
    return {'status': 'success'}


def handle_finish_errors(func):
    """Decorator that catches exceptions raised in model methods and raises
    appropriate `HTTPError` exceptions.
    """
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except errors.VersionNotPendingError:
            raise make_error(httplib.BAD_REQUEST, 'No pending upload')
        except errors.PendingSignatureMismatchError:
            raise make_error(httplib.BAD_REQUEST, 'Invalid upload signature')
    return wrapped


@handle_finish_errors
def finish_upload_success(file_record, payload):
    """
    :param FileRecord file_record: File record to resolve
    :param dict payload: Webhook payload from upload service
    :raise: `HTTPError`; see `handle_finish_errors`
    """
    file_record.resolve_pending_version(
        payload['uploadSignature'],
        payload['location'],
        payload['metadata'],
    )
    return {'status': 'success'}


@handle_finish_errors
def finish_upload_error(file_record, payload):
    """
    :param FileRecord file_record: File record to cancel
    :param dict payload: Webhook payload from upload service
    :raise: `HTTPError`; see `handle_finish_errors`
    """
    file_record.cancel_pending_version(payload['uploadSignature'])
    return {'status': 'success'}


@must_be_valid_project
@must_have_addon('osfstorage', 'node')
def osf_storage_upload_finish_hook(path, node_addon, **kwargs):
    """
    :raise: `HTTPError` if HMAC signature invalid, no upload pending, or
        upload already resolved
    """
    payload = get_payload_from_request(utils.webhook_signer, request)
    status = payload.get('status')
    if status not in ['success', 'error']:
        logger.error('Invalid status: {!r}'.format(status))
        raise make_error(httplib.BAD_REQUEST, 'Invalid status')
    file_record = model.FileRecord.find_by_path(path, node_addon)
    if file_record is None:
        raise HTTPError(httplib.NOT_FOUND)
    if status == 'success':
        return finish_upload_success(file_record, payload)
    return finish_upload_error(file_record, payload)


UPLOAD_PENDING_ERROR = HTTPError(
    httplib.NOT_FOUND,
    data={
        'message_short': 'File upload in progress',
        'message_long': (
            'File upload is in progress. Please check back later to retrieve '
            'this file.'
        ),
    }
)


UPLOAD_FAILED_ERROR = HTTPError(
    httplib.NOT_FOUND,
    data={
        'message_short': 'File upload failed',
        'message_long': (
            'File upload has failed. If this should not have occurred and the '
            'issue persists, please report it to '
            '<a href="mailto:support@osf.io">support@osf.io</a>.'
        ),
    }
)


def parse_version_specifier(version_str):
    """
    :raise: `InvalidVersionError` if version specifier cannot be parsed
    """
    try:
        version_idx = int(version_str)
    except (TypeError, ValueError):
        raise errors.InvalidVersionError
    if version_idx < 1:
        raise errors.InvalidVersionError
    return version_idx


def handle_incomplete_version(file_version):
    """
    :raise: `HTTPError` if version is incomplete
    """
    if file_version.status == model.status['PENDING']:
        raise UPLOAD_PENDING_ERROR
    if file_version.status == model.status['FAILED']:
        raise UPLOAD_FAILED_ERROR


def get_version_helper(file_record, version_str):
    """
    :return: Tuple of (version_index, file_version); note that index is one-based
    :raise: `HTTPError` if version specifier is invalid or version not found
    """
    if version_str is None:
        return (
            len(file_record.versions),
            file_record.versions[-1],
        )
    try:
        version_idx = parse_version_specifier(version_str)
    except errors.InvalidVersionError:
        raise make_error(httplib.BAD_REQUEST, 'Invalid version')
    try:
        return version_idx, file_record.versions[version_idx - 1]
    except IndexError:
        raise HTTPError(httplib.NOT_FOUND)


def get_version(path, node_settings, version_str):
    """
    :return: Tuple of (<one-based version index>, <file version>, <file record>)
    """
    record = model.FileRecord.find_by_path(path, node_settings)
    if record is None:
        raise HTTPError(httplib.NOT_FOUND)
    version_idx, file_version = get_version_helper(record, version_str)
    handle_incomplete_version(file_version)
    return version_idx, file_version, record


def serialize_file(idx, version, record, path, node):
    """Serialize data used to render a file.
    """
    rendered = utils.render_file(idx, version, record)
    return {
        'file_name': record.name,
        'file_revision': 'Version {0}'.format(idx),
        'file_path': record.path,
        'rendered': rendered,
        'files_url': node.web_url_for('collect_file_trees'),
        'download_url': node.web_url_for('osf_storage_download_file', path=path),
        'delete_url': node.api_url_for('osf_storage_delete_file', path=path),
        'revisions_url': node.api_url_for(
            'osf_storage_get_revisions',
            path=path,
        ),
        'render_url': node.api_url_for(
            'osf_storage_render_file',
            path=path,
            version=idx,
        ),
    }


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_view_file(auth, path, node_addon, **kwargs):
    node = node_addon.owner
    version = request.args.get('version')
    idx, version, record = get_version(path, node_addon, version)
    file_obj = model.StorageFile.get_or_create(node=node, path=path)
    redirect_url = check_file_guid(file_obj)
    if redirect_url:
        return redirect(redirect_url)
    ret = serialize_file(idx, version, record, path, node)
    ret.update(serialize_node(node, auth, primary=True))
    return ret


def update_analytics(node, path, version_idx):
    """
    :param Node node: Root node to update
    :param str path: Path to file
    :param int version_idx: One-based version index
    """
    update_counter('download:{0}:{1}'.format(node._id, path))
    update_counter('download:{0}:{1}:{2}'.format(node._id, path, version_idx))


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_download_file(path, node_addon, **kwargs):
    version_query = request.args.get('version')
    version_idx, version, record = get_version(path, node_addon, version_query)
    url = utils.get_download_url(version_idx, version, record)
    update_analytics(node_addon.owner, path, version_idx)
    return redirect(url)


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_render_file(path, node_addon, **kwargs):
    version = request.args.get('version')
    idx, version, record = get_version(path, node_addon, version)
    return utils.render_file(idx, version, record)


@must_be_contributor
@must_have_permission('write')
@must_have_addon('osfstorage', 'node')
def osf_storage_delete_file(auth, path, node_addon, **kwargs):
    file_record = model.FileRecord.find_by_path(path, node_addon)
    if file_record is None:
        raise HTTPError(httplib.NOT_FOUND)
    try:
        file_record.delete(auth)
    except errors.DeleteError:
        raise HTTPError(httplib.NOT_FOUND)
    file_record.save()
    return {'status': 'success'}


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_hgrid_contents(auth, node_addon, **kwargs):
    path = kwargs.get('path', '')
    file_tree = model.FileTree.find_by_path(path, node_addon)
    if file_tree is None:
        if path == '':
            return []
        raise HTTPError(httplib.NOT_FOUND)
    node = node_addon.owner
    permissions = utils.get_permissions(auth, node)
    return [
        utils.serialize_metadata_hgrid(item, node, permissions)
        for item in file_tree.children
        if not item.is_deleted
    ]


def osf_storage_root(node_settings, auth, **kwargs):
    """Build HGrid JSON for root node. Note: include node URLs for client-side
    URL creation for uploaded files.
    """
    node = node_settings.owner
    root = rubeus.build_addon_root(
        node_settings=node_settings,
        name='',
        permissions=auth,
        urls={
            'upload': node.api_url_for('osf_storage_request_upload_url'),
            'fetch': node.api_url_for('osf_storage_hgrid_contents'),
        },
        nodeUrl=node.url,
        nodeApiUrl=node.api_url,
    )
    return [root]


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_get_revisions(path, node_addon, **kwargs):
    node = node_addon.owner
    page = request.args.get('page', 0)
    try:
        page = int(page)
    except (TypeError, ValueError):
        raise HTTPError(httplib.BAD_REQUEST)
    record = model.FileRecord.find_by_path(path, node_addon)
    if record is None:
        raise HTTPError(httplib.NOT_FOUND)
    indices, versions, more = record.get_versions(
        page,
        size=osf_storage_settings.REVISIONS_PAGE_SIZE,
    )
    return {
        'revisions': [
            utils.serialize_revision(node, record, versions[idx], indices[idx])
            for idx in range(len(versions))
        ],
        'more': more,
    }


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_view_file_legacy(fid, node_addon, **kwargs):
    node = node_addon.owner
    return redirect(
        node.web_url_for(
            'osf_storage_view_file',
            path=fid,
        ),
        code=httplib.MOVED_PERMANENTLY,
    )


@must_be_contributor_or_public
@must_have_addon('osfstorage', 'node')
def osf_storage_download_file_legacy(fid, node_addon, **kwargs):
    node = node_addon.owner
    version = kwargs.get('vid', None)
    return redirect(
        node.web_url_for(
            'osf_storage_download_file',
            path=fid,
            version=version,
        ),
        code=httplib.MOVED_PERMANENTLY,
    )
