"""In-memory multipart encoding and parsing for audio uploads."""
from email import policy
from email.parser import BytesParser
import uuid

FIELDS = {"file", "model", "prompt", "language", "response_format", "temperature"}


def parse(content_type, body):
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body)
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart():
        raise ValueError("body must be multipart/form-data with a boundary")
    fields = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data" or part.is_multipart():
            raise ValueError("invalid multipart field")
        name = part.get_param("name", header="content-disposition")
        if name not in FIELDS:
            continue
        if name in fields:
            raise ValueError("duplicate field '%s'" % name)
        data = part.get_payload(decode=True)
        if data is None:
            raise ValueError("invalid multipart field")
        if name == "file":
            fields[name] = (part.get_filename() or "audio", data, part.get_content_type())
        else:
            fields[name] = data.decode("utf-8")
        if part.defects:
            raise ValueError("malformed multipart body")
    if message.defects:
        raise ValueError("malformed multipart body")
    return fields


def encode(fields):
    boundary = "pocket-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        if name not in FIELDS:
            continue
        chunks.append(("--%s\r\n" % boundary).encode())
        header = 'Content-Disposition: form-data; name="%s"' % name
        if name == "file":
            filename, data, content_type = value
            filename = filename.replace("\\", "\\\\").replace('"', '\\"')
            filename = filename.replace("\r", "").replace("\n", "")
            header += '; filename="%s"' % filename
            content_type = content_type.replace("\r", "").replace("\n", "")
            header += "\r\nContent-Type: " + content_type
        else:
            data = str(value).encode("utf-8")
        chunks.extend([header.encode("utf-8") + b"\r\n\r\n", data, b"\r\n"])
    chunks.append(("--%s--\r\n" % boundary).encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary
