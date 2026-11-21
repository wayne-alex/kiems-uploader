# api/views_iebc_proxy.py
"""
Reverse proxy for the IEBC public verification portal.

Why this exists: the IEBC portal sends `X-Frame-Options: sameorigin`,
which stops it from being embedded inside the KIEMS PWA's iframe.

This view fetches the page server-side, strips the blocking headers,
rewrites asset URLs so they resolve through this proxy, and returns
the modified HTML/CSS/JS.

It does NOT:
  - bypass the reCAPTCHA (impossible — that's the whole point of it)
  - allow the client to read the page's contents (iframe stays cross-origin
    from your PWA domain)
  - allow styling overrides to persist reliably (fragile by design)
"""

import re
from urllib.parse import urljoin, urlparse

import httpx
from django.conf import settings
from django.http import HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


# Headers we strip from the upstream response
STRIP_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "strict-transport-security",
    "content-encoding",     # we handle decompression ourselves
    "transfer-encoding",
    "connection",
}

# Headers we do not forward from the client request
STRIP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "accept-encoding",      # let httpx negotiate
    "cookie",               # we deliberately don't forward session cookies
    "referer",
    "origin",
}


def _upstream_url(path: str) -> str:
    base = settings.IEBC_UPSTREAM.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _rewrite_html(html: str, proxy_root: str, upstream_root: str) -> str:
    """
    Rewrite absolute URLs in the HTML so they pass through the proxy.

    - Absolute URLs pointing at upstream → proxy_root + path
    - Root-relative paths (/foo) → proxy_root + /foo
    - Protocol-relative (//verify.iebc.or.ke/...) → proxy_root + path
    - Inline <style> blocks: also rewrite url(...) references

    We do NOT attempt to rewrite JS-constructed URLs. If IEBC uses
    client-side routing or dynamic fetches to absolute URLs, those calls
    will bypass the proxy and hit the real IEBC server (and be blocked
    by CORS on the browser side). That's a limitation of any HTML proxy.
    """
    upstream_host = urlparse(upstream_root).netloc  # e.g. verify.iebc.or.ke

    # 1. Full absolute URLs to the upstream host
    html = re.sub(
        rf'https?://{re.escape(upstream_host)}(?P<path>/[^\s"\'<>]*)',
        lambda m: proxy_root + m.group("path"),
        html,
    )

    # 2. Protocol-relative //upstream/path
    html = re.sub(
        rf'//{re.escape(upstream_host)}(?P<path>/[^\s"\'<>]*)',
        lambda m: proxy_root + m.group("path"),
        html,
    )

    # 3. Root-relative URLs in src=, href=, action= attributes
    #    (skip if they already point to the proxy root)
    def _attr_sub(m):
        attr = m.group("attr")
        path = m.group("path")
        if path.startswith(proxy_root):
            return m.group(0)
        return f'{attr}="{proxy_root}{path}"'

    html = re.sub(
        r'(?P<attr>src|href|action)="(?P<path>/[^"#?]*)"',
        _attr_sub,
        html,
    )

    # 4. url(...) inside inline styles / <style> blocks
    def _css_url_sub(m):
        path = m.group("path")
        if path.startswith(proxy_root) or path.startswith("data:") or path.startswith("http"):
            return m.group(0)
        return f"url({proxy_root}{path})"

    html = re.sub(
        r'url\((["\']?)(?P<path>/[^)"\']*)\1\)',
        _css_url_sub,
        html,
    )

    return html


def _rewrite_css(css: str, proxy_root: str, upstream_root: str, css_url: str) -> str:
    """Rewrite url(...) and @import in a CSS file."""
    upstream_host = urlparse(upstream_root).netloc
    upstream_base = css_url.rsplit("/", 1)[0]

    # url(/absolute)
    def _url_sub(m):
        path = m.group("path")
        if path.startswith("data:") or path.startswith("http"):
            return m.group(0)
        if path.startswith("/"):
            return f'url("{proxy_root}{path}")'
        # relative to this CSS file
        absolute = urljoin(upstream_base + "/", path)
        parsed = urlparse(absolute)
        if parsed.netloc == upstream_host:
            return f'url("{proxy_root}{parsed.path}")'
        return m.group(0)

    css = re.sub(r'url\((["\']?)(?P<path>[^)"\']+)\1\)', _url_sub, css)

    # @import "/path" or @import url(...)
    def _import_sub(m):
        path = m.group("path")
        if path.startswith("/"):
            return f'@import "{proxy_root}{path}"'
        return m.group(0)

    css = re.sub(r'@import\s+(?:url\()?["\']?(?P<path>/[^"\')\s]+)', _import_sub, css)

    return css


@csrf_exempt
@require_http_methods(["GET", "POST", "HEAD"])
def iebc_proxy(request, path=""):
    """
    Reverse proxy for IEBC verification portal.

    URL:  /iebc-proxy/<path>     (path may be empty)
    E.g.: /iebc-proxy/           → https://verify.iebc.or.ke/
          /iebc-proxy/assets/x   → https://verify.iebc.or.ke/assets/x
    """
    if not getattr(settings, "IEBC_PROXY_ENABLED", False):
        raise Http404("IEBC proxy is disabled.")

    proxy_root = "/iebc-proxy"

    if not path.startswith("/"):
        path = "/" + path
    upstream_url = _upstream_url(path)

    # Prepare request to upstream
    req_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in STRIP_REQUEST_HEADERS
    }
    req_headers["User-Agent"] = settings.IEBC_PROXY_UA
    req_headers["Accept-Encoding"] = "identity"  # avoid gzip surprises

    body = request.body if request.method in ("POST", "PUT", "PATCH") else None

    try:
        with httpx.Client(
            timeout=settings.IEBC_PROXY_TIMEOUT,
            follow_redirects=True,
            verify=True,
        ) as client:
            upstream = client.request(
                method=request.method,
                url=upstream_url,
                headers=req_headers,
                content=body,
                params=request.GET.dict(),
            )
    except httpx.RequestError as exc:
        return HttpResponse(f"Upstream error: {exc}", status=502)

    # If upstream returned a redirect to another IEBC path, rewrite it
    if 300 <= upstream.status_code < 400 and "location" in upstream.headers:
        loc = upstream.headers["location"]
        if loc.startswith("/"):
            loc = proxy_root + loc
        elif "verify.iebc.or.ke" in loc:
            parsed = urlparse(loc)
            loc = proxy_root + parsed.path + (("?" + parsed.query) if parsed.query else "")
        # else leave external redirects alone

        resp = HttpResponse(status=upstream.status_code)
        resp["Location"] = loc
        return resp

    # Prepare response — strip blocking headers
    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in STRIP_RESPONSE_HEADERS
    }

    content_type = upstream.headers.get("content-type", "").lower()
    body = upstream.content

    # Rewrite HTML
    if "text/html" in content_type:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = body.decode("latin-1", errors="replace")
        text = _rewrite_html(text, proxy_root, settings.IEBC_UPSTREAM)
        body = text.encode("utf-8")
        response_headers["content-type"] = "text/html; charset=utf-8"

    # Rewrite CSS
    elif "text/css" in content_type:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = body.decode("latin-1", errors="replace")
        text = _rewrite_css(
            text, proxy_root, settings.IEBC_UPSTREAM, upstream_url
        )
        body = text.encode("utf-8")
        response_headers["content-type"] = "text/css; charset=utf-8"

    # For JS, JSON, images, fonts — pass through unchanged

    # Force a permissive CSP so the iframe can be embedded by your PWA
    # (the browser will complain otherwise, since you removed IEBC's own)
    response_headers["content-security-policy"] = (
        "frame-ancestors *; "  # allow anyone to embed (adjust to your PWA domain in production)
    )

    resp = HttpResponse(
        content=body,
        status=upstream.status_code,
        content_type=response_headers.pop("content-type", "application/octet-stream"),
    )
    for k, v in response_headers.items():
        # Skip a few that Django will set itself or that would confuse it
        if k.lower() in {"content-length", "set-cookie"}:
            continue
        try:
            resp[k] = v
        except Exception:
            pass  # some header values may be rejected by Django

    # IMPORTANT: do NOT forward upstream cookies to the client. reCAPTCHA
    # may set cookies on the IEBC domain; if you forward them under your
    # proxy domain, the browser will store them under YOUR domain, which
    # is a subtle privacy/session issue. Let the browser start clean.
    return resp