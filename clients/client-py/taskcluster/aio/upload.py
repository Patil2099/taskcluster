"""
Support for uploading objects to the object service, following best
practices for that service.

Data for upload is read from a "reader" provided by a "reader factory".  A
reader has an async `read(max_size)` method which reads and returns a chunk of
1 .. `max_size` bytes, or returns an empty string at EOF.  A reader factory is an async
callable which returns a fresh reader, ready to read the first byte of the
object.  When uploads are retried, the reader factory may be called more than
once.

Note that `aiofile.open` returns a value suitable for use as a reader, if async
file IO is important to the application.

This module provides several pre-defined readers and reader factories for
common cases.
"""
import six

if six.PY2:
    raise ImportError("upload is only supported in Python 3")

import base64

import aiohttp

import taskcluster
from taskcluster.upload import HashingReader as SyncHashingReader
from .reader_writer import streamingCopy, BufferReader, BufferWriter, FileReader
from .retry import retry

DATA_INLINE_MAX_SIZE = 8192


async def upload_from_buf(*, data, **kwargs):
    """
    Convenience method to upload data from an in-memory buffer.  Arguments are the same
    as `upload` except that `readerFactory` should not be supplied.
    """
    async def readerFactory():
        return BufferReader(data)

    await upload(**kwargs, readerFactory=readerFactory)


async def upload_from_file(*, file, **kwargs):
    """
    Convenience method to upload data from a a file.  The file should be open
    for reading, in binary mode, and be seekable (`f.seek`).  Remaining
    arguments are the same as `upload` except that `readerFactory` should not
    be supplied.
    """
    async def readerFactory():
        file.seek(0)
        return FileReader(file)

    await upload(**kwargs, readerFactory=readerFactory)


async def upload(*, projectId, name, contentType, contentLength, expires,
                 readerFactory, maxRetries=5, objectService):
    """
    Upload the given data to the object service with the given metadata.
    The `maxRetries` parameter has the same meaning as for service clients.
    The `objectService` parameter is an instance of the Object class,
    configured with credentials for the upload.
    """
    # wrap the readerFactory with one that will also hash the data
    hashingReader = None

    async def hashingReaderFactory():
        nonlocal hashingReader
        hashingReader = HashingReader(await readerFactory())
        return hashingReader

    async with aiohttp.ClientSession() as session:
        uploadId = taskcluster.slugid.nice()
        proposedUploadMethods = {}

        if contentLength < DATA_INLINE_MAX_SIZE:
            reader = await hashingReaderFactory()
            writer = BufferWriter()
            await streamingCopy(reader, writer)
            encoded = base64.b64encode(writer.getbuffer())
            proposedUploadMethods['dataInline'] = {
                "contentType": contentType,
                "objectData": encoded,
            }

        proposedUploadMethods['putUrl'] = {
            "contentType": contentType,
            "contentLength": contentLength,
        }

        uploadResp = await objectService.createUpload(name, {
            "expires": expires,
            "projectId": projectId,
            "uploadId": uploadId,
            "proposedUploadMethods": proposedUploadMethods,
        })

        async def tryUpload(retryFor):
            try:
                uploadMethod = uploadResp["uploadMethod"]
                if 'dataInline' in uploadMethod:
                    # data is already uploaded -- nothing to do
                    pass
                elif 'putUrl' in uploadMethod:
                    reader = await hashingReaderFactory()
                    await _putUrlUpload(uploadMethod['putUrl'], reader, session)
                else:
                    raise RuntimeError("Could not negotiate an upload method")
            except aiohttp.ClientResponseError as exc:
                # treat 4xx's as fatal, and retry others
                if 400 <= exc.status < 500:
                    raise exc
                return retryFor(exc)
            except aiohttp.ClientError as exc:
                # retry for all other aiohttp errors
                return retryFor(exc)
            # .. anything else is considered fatal

        await retry(maxRetries, tryUpload)

        # TODO: pass this value to finishUpload when the deployed instance supports it
        # https://github.com/taskcluster/taskcluster/issues/4714
        hashingReader.hashes(contentLength)

        await objectService.finishUpload(name, {
            "projectId": projectId,
            "uploadId": uploadId,
        })


async def _putUrlUpload(method, reader, session):
    chunk_size = 64 * 1024

    async def reader_gen():
        while True:
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            yield chunk

    resp = await session.put(method['url'], headers=method['headers'], data=reader_gen())
    resp.raise_for_status()


class HashingReader(SyncHashingReader):
    """An async version of the sync HashingReader"""

    async def read(self, max_size):
        chunk = await self.inner.read(max_size)
        self.update(chunk)
        return chunk
