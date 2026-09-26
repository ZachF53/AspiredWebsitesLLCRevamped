"""
Private file storage for client documents.

ClientDocument files (contracts, brand assets, anything Moonieful syncs
over) used to live under MEDIA_ROOT, which Nginx serves at /media/ with no
authentication — anyone holding a URL could read another client's files.
They now live under settings.PRIVATE_MEDIA_ROOT, which nothing serves
directly. The only way out is through the auth-checked download views
(admin_dashboard.v2.views_websites.website_document_download and
clients.views.portal_document_download).

The storage is handed to the FileField as a callable so the absolute path
never ends up baked into a migration.
"""

import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateDocumentStorage(FileSystemStorage):
    """FileSystemStorage with no public URL.

    FileSystemStorage falls back to MEDIA_URL when base_url is None, which
    would hand out /media/... links that look valid. Raising instead makes
    any template still using `doc.file.url` fail loudly in tests.
    """

    # Read PRIVATE_MEDIA_ROOT on every access rather than caching it at
    # model load (Django evaluates a callable `storage=` exactly once),
    # so override_settings(PRIVATE_MEDIA_ROOT=tmpdir) works in tests.
    @property
    def base_location(self):
        return str(settings.PRIVATE_MEDIA_ROOT)

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError(
            'Private documents have no public URL — link to the '
            'document download view instead.')


def private_document_storage():
    return PrivateDocumentStorage()
