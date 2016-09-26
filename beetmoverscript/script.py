#!/usr/bin/env python
"""Beetmover script
"""
from copy import deepcopy

import aiohttp
import asyncio
import logging
import os
import sys
import traceback
import boto3
import mimetypes

from scriptworker.client import get_task, validate_artifact_url
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException, ScriptWorkerRetryException
from scriptworker.utils import retry_async, download_file, upload_file

from beetmoverscript.constants import MIME_MAP
from beetmoverscript.task import validate_task_schema
from beetmoverscript.utils import load_json, generate_candidates_manifest

log = logging.getLogger(__name__)


# async_main {{{1
async def async_main(context):
    log.info("Hello Scriptworker!")
    # 1. parse the task
    context.task = get_task(context.config)  # e.g. $cfg['work_dir']/task.json
    # 2. validate the task
    validate_task_schema(context)
    # 3. generate manifest
    manifest = generate_candidates_manifest(context)
    # 4. for each artifact in manifest
    #   a. download artifact
    #   b. upload to candidates/dated location
    await beetmove_bits(context, manifest)
    # 5. copy to releases/latest location
    log.info('Success!')


async def beetmove_bits(context, manifest):
    for locale in manifest['mapping']:
        for deliverable in manifest['mapping'][locale]:
            source = os.path.join(manifest["artifact_base_url"],
                                  manifest['mapping'][locale][deliverable]['artifact'])
            dest_dated = os.path.join(manifest["s3_prefix_dated"],
                                      manifest['mapping'][locale][deliverable]['s3_key'])
            dest_latest = os.path.join(manifest["s3_prefix_latest"],
                                      manifest['mapping'][locale][deliverable]['s3_key'])
            await beetmove_bit(context, source, destinations=(dest_dated, dest_latest))


async def beetmove_bit(context, source, destinations):
    beet_config = deepcopy(context.config)
    beet_config.setdefault('valid_artifact_task_ids', context.task['dependencies'])

    rel_path = validate_artifact_url(beet_config, source)
    abs_file_path = os.path.join(context.config['work_dir'], rel_path)

    # TODO rather than upload twice, use something like boto's bucket.copy_key
    #   probably via the awscli subproc directly.
    # For now, this will be faster than using copy_key() as boto would block
    await download(context=context, url=source, path=abs_file_path)
    for dest in destinations:
        await upload_to_s3(context=context, s3_key=dest, path=abs_file_path)


async def download(context, url, path):
    await retry_async(download_file, args=(context, url, path),
                      kwargs={'session': context.session})


async def upload_to_s3(context, s3_key, path):
    api_kwargs = {
        'Bucket': context.config['s3']['bucket'],
        'Key': s3_key,
        'ContentType': mimetypes.guess_type(path)
    }
    headers = {
        'Content-Type': mimetypes.guess_type(path)
    }
    creds = context.config['s3']['credentials']
    s3 = boto3.client('s3', aws_access_key_id=creds['id'], aws_secret_access_key=creds['key'],)
    url = s3.generate_presigned_url('put_object', api_kwargs, expires_in=30, HttpMethod='PUT')

    await retry_async(upload_file, args=(context, url, headers, path),
                      kwargs={'session': context.session})


# main {{{1
def usage():
    print("Usage: {} CONFIG_FILE".format(sys.argv[0]), file=sys.stderr)
    sys.exit(1)


def setup_config(config_path):
    if config_path is None:
        if len(sys.argv) != 2:
            usage()
        config_path = sys.argv[1]
    context = Context()
    context.config = {}
    context.config.update(load_json(path=config_path))
    return context


def setup_logging():
    log_level = logging.DEBUG
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=log_level
    )
    logging.getLogger("taskcluster").setLevel(logging.WARNING)


def setup_mimetypes():
    mimetypes.init()
    map(lambda ext_mimetype: mimetypes.add_type(ext_mimetype[1], ext_mimetype[0]), MIME_MAP.items())


def main(name=None, config_path=None):
    if name not in (None, '__main__'):
        return

    context = setup_config(config_path)
    setup_logging()
    setup_mimetypes()

    loop = asyncio.get_event_loop()
    conn = aiohttp.TCPConnector()
    with aiohttp.ClientSession(connector=conn) as session:
        context.session = session
        try:
            loop.run_until_complete(async_main(context))
        except ScriptWorkerTaskException as exc:
            traceback.print_exc()
            sys.exit(exc.exit_code)
    loop.close()

main(name=__name__)
