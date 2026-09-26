"""
The one way a ClientDocument's bytes leave the server.

Both the admin (v2 Files tab) and the client portal download views call
document_download_response(); neither ever links to a storage URL (the
private storage has none — see clients/storage.py).

Every response is forced to download (Content-Disposition: attachment),
never sniffed (nosniff), and sandboxed (CSP `sandbox`), so an SVG or any
other scriptable file Moonieful or a client uploaded can never execute on
this origin even if a browser decides to render it.
"""

from django.http import FileResponse, Http404

SANDBOX_CSP = "sandbox; default-src 'none'"


def document_download_response(doc):
    if not doc.file:
        raise Http404('This file has not arrived yet.')
    try:
        fh = doc.file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404('File not found on disk.')
    response = FileResponse(
        fh, as_attachment=True, filename=doc.filename or 'download',
        content_type='application/octet-stream',
    )
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = SANDBOX_CSP
    response['Cache-Control'] = 'private, no-store'
    # core.middleware.SecurityHeadersMiddleware leaves this CSP alone.
    response.keep_csp = True
    return response
