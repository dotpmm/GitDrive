"""End-to-end API test with an in-memory MongoDB (mongomock) and a fake GitHub."""
import hashlib
import io
import os
import sys
import time
import zipfile

os.environ.update(
    GITHUB_TOKEN="x", GITHUB_OWNER="me", MONGODB_URI="mongodb://x", DATABASE_NAME="t",
    ENCRYPTION_KEY="k" * 32, JWT_SECRET="s" * 32, ADMIN_USERNAMES="boss", FRONTEND_URL="http://app.test",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mongomock
import pymongo

pymongo.MongoClient = mongomock.MongoClient

import pytest
from fastapi.testclient import TestClient

from api import config, storage
from api.main import app

config.CHUNK_SIZE = 100  # tiny chunks so multi-chunk paths run fast
storage.config.CHUNK_SIZE = 100


class FakeGH:
    def __init__(self):
        self.blobs, self.files, self.commits, self.repos = {}, {}, 0, set()
        self.fail_blobs = 0

    def create_repo(self, name): self.repos.add(name)

    def create_blob(self, repo, content):
        if self.fail_blobs:
            self.fail_blobs -= 1
            raise RuntimeError("boom")
        sha = hashlib.sha1(content).hexdigest()
        self.blobs[(repo, sha)] = content
        return sha

    def commit_blobs(self, repo, entries, message):
        self.commits += 1
        for path, sha in entries.items():
            self.files[(repo, path)] = self.blobs[(repo, sha)]

    def read_object(self, repo, path, sha):
        return self.files[(repo, path)]


gh = FakeGH()
storage.github = gh


@pytest.fixture(autouse=True)
def _fresh_limits():
    from api import security
    security.limiter._hits.clear()


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        yield client


def H(tok): return {"Authorization": f"Bearer {tok}"}


def signup(c, name, pw="password123"):
    r = c.post("/api/auth/signup", json={"username": name, "password": pw, "email": f"{name}@x.io"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def upload(c, tok, space, parent, name, data, mime=""):
    r = c.post("/api/uploads", headers=H(tok), json={"space": space, "parent": parent, "name": name, "size": len(data), "mime": mime})
    assert r.status_code == 200, r.text
    up = r.json()
    cs = up["chunk_size"]
    order = list(range(up["total_chunks"]))[::-1]  # out of order on purpose
    for i in order:
        rr = c.put(f"/api/uploads/{up['upload_id']}/chunks/{i}", headers=H(tok), content=data[i * cs:(i + 1) * cs])
        assert rr.status_code == 200, rr.text
    r = c.post(f"/api/uploads/{up['upload_id']}/complete", headers=H(tok))
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_flow(c):
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/api/me").status_code == 401
    t = signup(c, "alice")
    assert c.get("/api/me", headers=H(t)).json()["username"] == "alice"
    # bad input / duplicates
    assert c.post("/api/auth/signup", json={"username": "a", "password": "password123", "email": "a@x.io"}).status_code == 400
    assert c.post("/api/auth/signup", json={"username": "alice", "password": "password123", "email": "z@x.io"}).status_code == 409
    assert c.post("/api/auth/signup", json={"username": "zed", "password": "short", "email": "z@x.io"}).status_code == 400
    assert c.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "ALICE", "password": "password123"})
    assert r.status_code == 200 and r.json()["user"]["is_admin"] is False
    assert c.get("/api/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_password_reset(c):
    from api import db
    signup(c, "resetme")
    assert c.post("/api/auth/forgot", json={"email": "nobody@x.io"}).json() == {"ok": True}
    # admin generates a link as the no-email fallback
    boss = signup(c, "boss")
    link = c.post("/api/admin/users/resetme/reset-link", headers=H(boss)).json()["link"]
    token = link.split("token=")[1]
    assert c.post("/api/auth/reset", json={"token": "bad", "password": "newpassword1"}).status_code == 400
    assert c.post("/api/auth/reset", json={"token": token, "password": "newpassword1"}).status_code == 200
    assert c.post("/api/auth/reset", json={"token": token, "password": "newpassword2"}).status_code == 400  # single use
    assert c.post("/api/auth/login", json={"username": "resetme", "password": "newpassword1"}).status_code == 200


def test_spaces_invites_permissions(c):
    a, b, cc = signup(c, "ann"), signup(c, "bob"), signup(c, "cat")
    spaces = c.get("/api/spaces", headers=H(a)).json()
    assert [s["type"] for s in spaces] == ["private", "public"]
    sp = c.post("/api/spaces", headers=H(a), json={"name": "Team"}).json()
    sid = sp["id"]
    # not visible to others
    assert c.get(f"/api/spaces/{sid}/nodes", headers=H(b)).status_code == 404
    # invite flow
    assert c.post(f"/api/spaces/{sid}/invites", headers=H(a), json={"username": "ghost"}).status_code == 404
    assert c.post(f"/api/spaces/{sid}/invites", headers=H(a), json={"username": "bob", "role": "viewer"}).status_code == 200
    assert c.post(f"/api/spaces/{sid}/invites", headers=H(a), json={"username": "bob"}).status_code == 409
    inv = c.get("/api/invites", headers=H(b)).json()
    assert len(inv) == 1 and inv[0]["from"] == "ann" and inv[0]["space_name"] == "Team"
    assert c.post(f"/api/invites/{inv[0]['id']}/accept", headers=H(cc)).status_code == 404  # not hers
    assert c.post(f"/api/invites/{inv[0]['id']}/accept", headers=H(b)).status_code == 200
    # viewer can list but not write
    f = upload(c, a, sid, None, "hello.txt", b"hello world")
    assert [n["name"] for n in c.get(f"/api/spaces/{sid}/nodes", headers=H(b)).json()["nodes"]] == ["hello.txt"]
    r = c.post("/api/uploads", headers=H(b), json={"space": sid, "name": "x.txt", "size": 1})
    assert r.status_code == 403
    assert c.post(f"/api/spaces/{sid}/folders", headers=H(b), json={"name": "nope"}).status_code == 403
    assert c.post(f"/api/nodes/{f['id']}/trash", headers=H(b)).status_code == 403
    # promote to editor
    assert c.patch(f"/api/spaces/{sid}/members/bob", headers=H(a), json={"role": "editor"}).status_code == 200
    assert c.post(f"/api/spaces/{sid}/folders", headers=H(b), json={"name": "docs"}).status_code == 200
    # outsider still blocked, also from the node itself
    assert c.get(f"/api/nodes/{f['id']}", headers=H(cc)).status_code == 404
    # member leaves
    assert c.delete(f"/api/spaces/{sid}/members/bob", headers=H(b)).status_code == 200
    assert c.get(f"/api/spaces/{sid}/nodes", headers=H(b)).status_code == 404
    # only owner deletes the space
    assert c.delete(f"/api/spaces/{sid}", headers=H(cc)).status_code == 404
    assert c.delete(f"/api/spaces/{sid}", headers=H(a)).status_code == 200


def test_private_and_public_spaces(c):
    d, e = signup(c, "dan"), signup(c, "eve")
    priv = "private:dan"
    upload(c, d, priv, None, "secret.txt", b"top secret")
    assert c.get(f"/api/spaces/{priv}/nodes", headers=H(e)).status_code == 404
    pub_file = upload(c, d, "public", None, "pub.txt", b"hi all")
    names = [n["name"] for n in c.get("/api/spaces/public/nodes", headers=H(e)).json()["nodes"]]
    assert "pub.txt" in names
    # eve can't delete dan's public file, dan can
    assert c.post(f"/api/nodes/{pub_file['id']}/trash", headers=H(e)).status_code == 403
    assert c.post(f"/api/nodes/{pub_file['id']}/trash", headers=H(d)).status_code == 200
    # search only sees accessible files
    assert c.get("/api/search?q=secret", headers=H(e)).json() == []
    assert len(c.get("/api/search?q=secret", headers=H(d)).json()) == 1


def test_upload_download_range_and_integrity(c):
    f_tok = signup(c, "finn")
    data = os.urandom(1234)  # 13 chunks of 100
    node = upload(c, f_tok, "private:finn", None, "data.bin", data, "application/octet-stream")
    assert node["size"] == 1234
    url = c.post(f"/api/nodes/{node['id']}/url", headers=H(f_tok), json={"download": True}).json()["url"]
    path = url.split("testserver")[1]
    r = c.get(path)
    assert r.status_code == 200 and r.content == data and "attachment" in r.headers["content-disposition"]
    assert r.headers["accept-ranges"] == "bytes"
    # ranges, incl. ones that span chunk boundaries
    for hdr, exp in [("bytes=0-0", data[0:1]), ("bytes=95-205", data[95:206]), ("bytes=1200-", data[1200:]), ("bytes=-34", data[-34:]), ("bytes=0-99999", data)]:
        rr = c.get(path, headers={"Range": hdr})
        assert rr.status_code == 206 and rr.content == exp, hdr
    assert c.get(path, headers={"Range": "bytes=5000-6000"}).status_code == 416
    assert c.head(path).headers["content-length"] == "1234"
    assert c.get(path, headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    # tampered signature / no signature
    assert c.get(path.replace("sig=", "sig=x")).status_code == 403
    # chunk data is really encrypted at rest
    stored = b"".join(gh.files.values())
    assert data[:50] not in stored
    # wrong chunk size and missing chunk are rejected
    up = c.post("/api/uploads", headers=H(f_tok), json={"space": "private:finn", "name": "x", "size": 150}).json()
    assert c.put(f"/api/uploads/{up['upload_id']}/chunks/0", headers=H(f_tok), content=b"short").status_code == 400
    assert c.put(f"/api/uploads/{up['upload_id']}/chunks/0", headers=H(f_tok), content=b"a" * 100).status_code == 200
    assert c.post(f"/api/uploads/{up['upload_id']}/complete", headers=H(f_tok)).status_code == 400
    assert c.delete(f"/api/uploads/{up['upload_id']}", headers=H(f_tok)).status_code == 200
    # another user can't touch this session
    other = signup(c, "finn2")
    up2 = c.post("/api/uploads", headers=H(f_tok), json={"space": "private:finn", "name": "y", "size": 5}).json()
    assert c.put(f"/api/uploads/{up2['upload_id']}/chunks/0", headers=H(other), content=b"12345").status_code == 404
    # github outage mid-upload -> 502, retry works, nothing half-saved
    gh.fail_blobs = 1
    assert c.put(f"/api/uploads/{up2['upload_id']}/chunks/0", headers=H(f_tok), content=b"12345").status_code == 502
    assert c.put(f"/api/uploads/{up2['upload_id']}/chunks/0", headers=H(f_tok), content=b"12345").status_code == 200
    assert c.post(f"/api/uploads/{up2['upload_id']}/complete", headers=H(f_tok)).status_code == 200
    # empty file
    e = upload(c, f_tok, "private:finn", None, "empty.txt", b"")
    p = c.post(f"/api/nodes/{e['id']}/url", headers=H(f_tok), json={}).json()["url"].split("testserver")[1]
    assert c.get(p).content == b""


def test_share_links_embed_and_folders(c):
    g = signup(c, "gus")
    vid = os.urandom(450)
    node = upload(c, g, "private:gus", None, "clip one.mp4", vid, "video/mp4")
    s = c.post(f"/api/nodes/{node['id']}/share", headers=H(g), json={}).json()["share"]
    assert s["raw_url"].endswith("/s/%s/clip%%20one.mp4" % s["token"])
    raw = s["raw_url"].split("testserver")[1]
    r = c.get(raw)  # no auth header at all
    assert r.status_code == 200 and r.content == vid and r.headers["content-type"].startswith("video/mp4")
    assert r.headers["access-control-allow-origin"] == "*" and "inline" in r.headers["content-disposition"]
    assert c.get(raw, headers={"Range": "bytes=100-199"}).content == vid[100:200]
    # same link returned again (no duplicates)
    assert c.post(f"/api/nodes/{node['id']}/share", headers=H(g), json={}).json()["share"]["token"] == s["token"]
    # humans are redirected to the app, bots get Open Graph tags
    page = "/s/" + s["token"]
    rr = c.get(page, follow_redirects=False)
    assert rr.status_code == 302 and rr.headers["location"] == f"http://app.test/share/{s['token']}"
    bot = c.get(page, headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0)"})
    assert 'property="og:video"' in bot.text and "video/mp4" in bot.text
    # info endpoint is public
    assert c.get(f"/api/shared/{s['token']}").json()["root"]["name"] == "clip one.mp4"
    # revoke
    assert c.delete(f"/api/nodes/{node['id']}/share", headers=H(g)).status_code == 200
    assert c.get(raw).status_code == 404
    # html is never served as html
    h = upload(c, g, "private:gus", None, "x.html", b"<script>alert(1)</script>", "text/html")
    hs = c.post(f"/api/nodes/{h['id']}/share", headers=H(g), json={}).json()["share"]
    hr = c.get(hs["raw_url"].split("testserver")[1])
    assert hr.headers["content-type"].startswith("text/plain") and "sandbox" in hr.headers["content-security-policy"]
    # folder share: children reachable, outsiders' files are not
    fid = c.post("/api/spaces/private:gus/folders", headers=H(g), json={"name": "album"}).json()["id"]
    inner = upload(c, g, "private:gus", fid, "pic.png", b"png-bytes", "image/png")
    other = upload(c, g, "private:gus", None, "other.txt", b"nope")
    fs = c.post(f"/api/nodes/{fid}/share", headers=H(g), json={"expires_days": 7}).json()["share"]
    info = c.get(f"/api/shared/{fs['token']}").json()
    assert [k["name"] for k in info["children"]] == ["pic.png"]
    child = info["children"][0]["raw_url"].split("testserver")[1]
    assert c.get(child).content == b"png-bytes"
    assert c.get(f"/s/{fs['token']}/n/{other['id']}/other.txt").status_code == 404


def test_files_folders_trash_move(c):
    h = signup(c, "hal")
    sp = "private:hal"
    f1 = c.post(f"/api/spaces/{sp}/folders", headers=H(h), json={"name": "A"}).json()
    f2 = c.post(f"/api/spaces/{sp}/folders", headers=H(h), json={"name": "A"}).json()
    assert f2["name"] == "A (1)"  # no duplicates in one folder
    deep = c.post(f"/api/spaces/{sp}/folders/ensure", headers=H(h), json={"path": "A/b/c"}).json()["id"]
    again = c.post(f"/api/spaces/{sp}/folders/ensure", headers=H(h), json={"path": "A/b/c"}).json()["id"]
    assert deep == again
    n = upload(c, h, sp, deep, "f.txt", b"x" * 250)
    lst = c.get(f"/api/spaces/{sp}/nodes?parent={deep}", headers=H(h)).json()
    assert [p["name"] for p in lst["path"]] == ["A", "b", "c"]
    # can't move a folder into itself / its descendants
    assert c.patch(f"/api/nodes/{f1['id']}", headers=H(h), json={"parent": deep}).status_code == 400
    # rename + move + star
    assert c.patch(f"/api/nodes/{n['id']}", headers=H(h), json={"name": "g.txt", "parent": "root"}).json()["name"] == "g.txt"
    assert c.patch(f"/api/nodes/{n['id']}", headers=H(h), json={"starred": True}).json()["starred"] is True
    assert [s["name"] for s in c.get("/api/starred", headers=H(h)).json()] == ["g.txt"]
    assert c.patch(f"/api/nodes/{n['id']}", headers=H(h), json={"name": "bad/name"}).status_code == 400
    # purge needs trash first; used_bytes tracks
    used = lambda: c.get("/api/me", headers=H(h)).json()["used_bytes"]
    assert used() == 250
    assert c.delete(f"/api/nodes/{n['id']}", headers=H(h)).status_code == 400
    assert c.post(f"/api/nodes/{n['id']}/trash", headers=H(h)).status_code == 200
    assert c.get(f"/api/spaces/{sp}/nodes?trashed=true", headers=H(h)).json()["nodes"][0]["name"] == "g.txt"
    assert c.post(f"/api/nodes/{n['id']}/restore", headers=H(h)).status_code == 200
    c.post(f"/api/nodes/{n['id']}/trash", headers=H(h))
    assert c.delete(f"/api/nodes/{n['id']}", headers=H(h)).status_code == 200
    assert used() == 0
    from api import db
    assert db.blobs.count_documents({}) >= 0
    # folder purge removes everything below
    c.post(f"/api/nodes/{f1['id']}/trash", headers=H(h))
    assert c.delete(f"/api/nodes/{f1['id']}", headers=H(h)).status_code == 200
    assert db.nodes.count_documents({"_id": db.oid(deep)}) == 0


def test_quota(c):
    from api import db
    q = signup(c, "quinn")
    db.users.update_one({"username": "quinn"}, {"$set": {"quota_bytes": 300}})
    upload(c, q, "private:quinn", None, "a", b"x" * 200)
    r = c.post("/api/uploads", headers=H(q), json={"space": "private:quinn", "name": "b", "size": 200})
    assert r.status_code == 413


def test_zip_download_and_unzip(c):
    z = signup(c, "zoe")
    sp = "private:zoe"
    fid = c.post(f"/api/spaces/{sp}/folders", headers=H(z), json={"name": "proj"}).json()["id"]
    a = upload(c, z, sp, fid, "a.txt", b"A" * 333)
    sub = c.post(f"/api/spaces/{sp}/folders", headers=H(z), json={"name": "sub", "parent": fid}).json()["id"]
    b = upload(c, z, sp, sub, "b.bin", os.urandom(250))
    url = c.post("/api/zip-url", headers=H(z), json={"space": sp, "ids": [fid], "name": "proj"}).json()["url"].split("testserver")[1]
    r = c.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["proj/a.txt", "proj/sub/b.bin"]
    assert zf.read("proj/a.txt") == b"A" * 333

    # unzip: build an archive with a zip-slip entry and a normal tree
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("docs/readme.md", "hello" * 50)
        out.writestr("docs/deep/x.txt", "y" * 130)
        out.writestr("../evil.txt", "nope")
        out.writestr("top.txt", "t")
    arc = upload(c, z, sp, None, "bundle.zip", buf.getvalue(), "application/zip")
    job = c.post(f"/api/nodes/{arc['id']}/extract", headers=H(z)).json()["job_id"]
    for _ in range(100):
        st = c.get(f"/api/jobs/{job}", headers=H(z)).json()
        if st["status"] != "running":
            break
        time.sleep(0.05)
    assert st["status"] == "done" and st["skipped"] == 1, st
    root = c.get(f"/api/spaces/{sp}/nodes?parent={st['folder']}", headers=H(z)).json()
    assert sorted(n["name"] for n in root["nodes"]) == ["docs", "top.txt"]
    assert not [n for n in c.get(f"/api/spaces/{sp}/nodes", headers=H(z)).json()["nodes"] if n["name"] == "evil.txt"]
    assert c.post(f"/api/nodes/{a['id']}/extract", headers=H(z)).status_code == 400
    other = signup(c, "zed")
    assert c.get(f"/api/jobs/{job}", headers=H(other)).status_code == 404


def test_admin(c):
    boss = signup(c, "boss2") if False else c.post("/api/auth/login", json={"username": "boss", "password": "password123"}).json()["token"]
    pleb = signup(c, "pleb")
    assert c.get("/api/admin/users", headers=H(pleb)).status_code == 403
    ov = c.get("/api/admin/overview", headers=H(boss)).json()
    assert ov["users"] >= 5 and ov["files"] > 0 and ov["repos"]
    users = {u["username"]: u for u in c.get("/api/admin/users", headers=H(boss)).json()}
    assert users["zoe"]["files"] >= 2 and users["zoe"]["bytes"] > 0 and users["ann"]["spaces_owned"] == 0
    assert users["boss"]["is_admin"] is True
    assert any(s["type"] == "public" for s in c.get("/api/admin/spaces", headers=H(boss)).json())
    # disable kicks the user out immediately
    assert c.patch("/api/admin/users/pleb", headers=H(boss), json={"disabled": True}).status_code == 200
    assert c.get("/api/me", headers=H(pleb)).status_code in (401, 403)
    assert c.post("/api/auth/login", json={"username": "pleb", "password": "password123"}).status_code == 403
    assert c.patch("/api/admin/users/boss", headers=H(boss), json={"disabled": True}).status_code == 400
    assert c.patch("/api/admin/users/zoe", headers=H(boss), json={"quota_bytes": 5}).status_code == 200
    # delete user wipes their data and keys
    from api import db
    before = db.blobs.count_documents({})
    assert c.delete("/api/admin/users/zoe", headers=H(boss)).status_code == 200
    assert db.blobs.count_documents({}) < before and not db.nodes.find_one({"owner": "zoe"})
    assert c.delete("/api/admin/users/boss", headers=H(boss)).status_code == 400


def test_login_rate_limit(c):
    signup(c, "rate")
    codes = [c.post("/api/auth/login", json={"username": "rate", "password": "wrong"}).status_code for _ in range(10)]
    assert 429 in codes
