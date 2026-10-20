"""S3 probe run with the container's python3; standard library only.

Usage:
  s3.py ping HOST PORT
  s3.py basic HOST PORT ACCESS SECRET
  s3.py create-bucket HOST PORT ACCESS SECRET BUCKET
  s3.py put HOST PORT ACCESS SECRET BUCKET KEY SEED SIZE
  s3.py get HOST PORT ACCESS SECRET BUCKET KEY SEED SIZE
"""
import datetime
import hashlib
import hmac
import http.client
import sys
import urllib.parse

REGION = "us-east-1"


def payload(seed, size):
    """Deterministic bytes, so separate processes and clusters agree on content."""
    count = (size + 31) // 32
    return b"".join(hashlib.sha256(("%s:%d" % (seed, index)).encode()).digest() for index in range(count))[:size]


def signing_key(secret, day):
    key = ("AWS4" + secret).encode()
    for part in (day, REGION, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def authorization(method, path, query, headers, body_hash, access, secret, now):
    """AWS Signature Version 4 header for the given request."""
    day = now.strftime("%Y%m%d")
    signed = ";".join(sorted(headers))
    canonical = "\n".join([
        method, urllib.parse.quote(path, safe="/~"), query,
        "".join(name + ":" + headers[name] + "\n" for name in sorted(headers)),
        signed, body_hash,
    ])
    scope = day + "/" + REGION + "/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", headers["x-amz-date"], scope,
        hashlib.sha256(canonical.encode()).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret, day), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (access, scope, signed, signature)


def request(host, port, method, path, access=None, secret=None, body=b"", query=""):
    headers = {"host": "%s:%s" % (host, port)}
    if access:
        now = datetime.datetime.utcnow()
        body_hash = hashlib.sha256(body).hexdigest()
        headers.update({"x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"), "x-amz-content-sha256": body_hash})
        headers["authorization"] = authorization(method, path, query, dict(headers), body_hash, access, secret, now)
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        connection.request(method, path + ("?" + query if query else ""), body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def expect(status, wanted, what, data=b""):
    if status not in wanted:
        raise SystemExit("%s: HTTP %d %s" % (what, status, data[:300]))


def basic(host, port, access, secret):
    bucket, key = "tc-basic", "objects/data.bin"
    data = payload("basic", 1 << 20)
    status, body = request(host, port, "PUT", "/" + bucket, access, secret)
    expect(status, (200,), "create bucket", body)
    status, _ = request(host, port, "PUT", "/%s/%s" % (bucket, key), access, secret, body=data)
    expect(status, (200,), "put object")
    status, body = request(host, port, "GET", "/%s/%s" % (bucket, key), access, secret)
    expect(status, (200,), "get object")
    if body != data:
        raise SystemExit("object bytes differ")
    status, body = request(host, port, "GET", "/" + bucket, access, secret, query="list-type=2")
    expect(status, (200,), "list objects")
    if b"<Key>" + key.encode() + b"</Key>" not in body:
        raise SystemExit("object missing from listing")
    status, body = request(host, port, "GET", "/%s/%s" % (bucket, key), access, secret + "x")
    expect(status, (403,), "wrong secret must be rejected", body)
    status, body = request(host, port, "GET", "/%s/%s" % (bucket, key))
    expect(status, (403,), "anonymous read must be rejected", body)
    expect(request(host, port, "DELETE", "/%s/%s" % (bucket, key), access, secret)[0], (204,), "delete object")
    status, _ = request(host, port, "GET", "/%s/%s" % (bucket, key), access, secret)
    expect(status, (404,), "deleted object must be gone")
    expect(request(host, port, "DELETE", "/" + bucket, access, secret)[0], (204,), "delete bucket")


def main(argv):
    mode, host, port = argv[1], argv[2], argv[3]
    if mode == "ping":
        status, _ = request(host, port, "GET", "/")
        expect(status, (200,), "anonymous service request")
    elif mode == "basic":
        basic(host, port, argv[4], argv[5])
    elif mode == "create-bucket":
        status, body = request(host, port, "PUT", "/" + argv[6], argv[4], argv[5])
        expect(status, (200,), "create bucket", body)
    elif mode in ("put", "get"):
        access, secret, bucket, key, seed, size = argv[4:10]
        data = payload(seed, int(size))
        if mode == "put":
            status, body = request(host, port, "PUT", "/%s/%s" % (bucket, key), access, secret, body=data)
            expect(status, (200,), "put object", body)
        else:
            status, body = request(host, port, "GET", "/%s/%s" % (bucket, key), access, secret)
            expect(status, (200,), "get object", body)
            if body != data:
                raise SystemExit("object bytes differ")
    else:
        raise SystemExit("unknown mode " + mode)
    print("ok")


if __name__ == "__main__":
    main(sys.argv)
