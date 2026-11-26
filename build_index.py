#!/usr/bin/env python3
"""Builds index.json for the KMod Launcher.

    python build_index.py                     # crawl GitHub repositories with the topic "kmod-mod"
    python build_index.py --local packages/   # index the .kmod files of a folder (testing, private indexes)

Standard library only. GITHUB_TOKEN (set by GitHub Actions) raises the API limits.
Output: index.json, icons/<id>.<ext>, covers/<id>.<ext> inside --out (default: the current folder).
Packages are only read as zip archives; nothing in them is ever executed.
"""
import argparse, datetime, hashlib, io, json, os, re, shutil, sys, urllib.parse, urllib.request, zipfile

TOPIC = "kmod-mod"
MAX_PACKAGE = 512 * 1024 * 1024  # packages may carry pak files
MAX_IMAGE = 512 * 1024
KEEP_VERSIONS = 5
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
ID_RE = re.compile(r"^[a-z0-9_]+$")


def log(*a):
    print(*a, file=sys.stderr)


def get(url, accept="application/vnd.github+json", limit=MAX_PACKAGE):
    headers = {"User-Agent": "kmod-index", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def api(path):
    return json.loads(get("https://api.github.com/" + path, limit=20 * 1024 * 1024))


def plain_path(name):
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith("/") and ":" not in name and ".." not in parts


def read_package(data):
    """-> (manifest, {"icon": (ext, bytes), "cover": ...}) or raises ValueError."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read("mod.json").decode("utf-8-sig"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"not a .kmod package: {e}")
    if not isinstance(manifest, dict) or not ID_RE.match(str(manifest.get("id", ""))) or not manifest.get("version"):
        raise ValueError("mod.json needs a valid id and a version")
    images = {}
    for kind in ("icon", "cover"):
        name = str(manifest.get(kind) or kind + ".png")
        ext = os.path.splitext(name)[1].lower()
        if not plain_path(name) or ext not in IMAGE_EXT:
            continue
        try:
            if z.getinfo(name).file_size <= MAX_IMAGE:
                images[kind] = (ext, z.read(name))
        except KeyError:
            pass
    return manifest, images


def platforms_of(m):
    p = m.get("platform")
    if not p:
        return ["Android", "Windows"]
    p = [p] if isinstance(p, str) else p
    return [x for x in ("Android", "Windows") if x.lower() in [str(v).lower() for v in p]]


def version_entry(m, data, url, published):
    return {
        "version": str(m["version"]),
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "published": published,
        "platforms": platforms_of(m),
        "native": bool(m.get("native")),
        "paks": bool(m.get("paks")),
        "permissions": [str(x) for x in m.get("permissions", [])],
        "depends": [str(x) for x in m.get("depends", [])],
    }


def mod_entry(m, images, out, versions, extra):
    e = {
        "id": m["id"],
        "name": str(m.get("name") or m["id"]),
        "author": str(m.get("author") or ""),
        "description": str(m.get("description") or ""),
        "versions": versions,
    }
    for kind, folder in (("icon", "icons"), ("cover", "covers")):
        if kind in images:
            ext, blob = images[kind]
            os.makedirs(os.path.join(out, folder), exist_ok=True)
            with open(os.path.join(out, folder, m["id"] + ext), "wb") as f:
                f.write(blob)
            e[kind] = f"{folder}/{m['id']}{ext}"
    e.update(extra)
    return e


def from_folder(folder, out):
    found = {}
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".kmod"):
            continue
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read()
        try:
            m, images = read_package(data)
        except ValueError as e:
            log(name, e)
            continue
        os.makedirs(os.path.join(out, "packages"), exist_ok=True)
        target = os.path.join(out, "packages", name)
        if os.path.abspath(target) != os.path.abspath(os.path.join(folder, name)):
            shutil.copyfile(os.path.join(folder, name), target)
        today = datetime.date.today().isoformat()
        v = version_entry(m, data, "packages/" + urllib.parse.quote(name), today)
        found[m["id"]] = mod_entry(m, images, out, [v], {"repo": "", "stars": 0, "updated": today})
    return list(found.values())


def downloads_of(asset):
    # the indexer itself downloads every package it lists exactly once
    return max(int(asset.get("download_count") or 0) - 1, 0)


def kmod_asset(rel):
    return next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".kmod") and a["size"] <= MAX_PACKAGE), None)


def reusable(before, asset, newest, out):
    """Can the entry of the previous run stand for this asset? Downloading again would count as a download."""
    if not before:
        return False
    mod, version = before
    if asset.get("digest") != "sha256:" + version["sha256"] or "paks" not in version:
        return False
    if not newest:
        return True
    # the mod's name, description and pictures come from its newest package
    return mod["versions"][0]["url"] == version["url"] and all(os.path.isfile(os.path.join(out, mod[k])) for k in ("icon", "cover") if mod.get(k))


def from_github(out, old):
    known = {v["url"]: (m, v) for m in old.get("mods", []) for v in m.get("versions", [])}
    repos, page = [], 1
    while page <= 10:
        r = api(f"search/repositories?q=topic:{TOPIC}&per_page=100&page={page}")
        repos += r.get("items", [])
        if len(r.get("items", [])) < 100:
            break
        page += 1
    repos.sort(key=lambda r: r["created_at"])  # the older repository keeps a contested id
    mods, taken = [], set()
    for repo in repos:
        full = repo["full_name"]
        try:
            releases = [r for r in api(f"repos/{full}/releases?per_page=100") if not r.get("draft")]
            versions, latest = [], None  # latest: (id, entry without versions or None, (manifest, images) or None)
            for rel in releases:
                if len(versions) >= KEEP_VERSIONS:
                    break
                asset = kmod_asset(rel)
                if not asset:
                    continue
                url = asset["browser_download_url"]
                published = rel.get("published_at", "")[:10]
                before = known.get(url)
                if reusable(before, asset, latest is None, out):
                    mod_id = before[0]["id"]
                    version = dict(before[1], published=published)
                    found = (mod_id, before[0], None)
                else:
                    data = get(url, accept="application/octet-stream")
                    m, images = read_package(data)
                    mod_id = m["id"]
                    if before and before[1]["sha256"] != hashlib.sha256(data).hexdigest():
                        log(full, "the file of", url, "changed after it was indexed")
                    version = version_entry(m, data, url, published)
                    found = (mod_id, None, (m, images))
                if latest and mod_id != latest[0]:
                    continue
                version["downloads"] = downloads_of(asset)
                versions.append(version)
                latest = latest or found
            if not latest:
                continue
            mod_id, previous, package = latest
            if mod_id in taken:
                log(full, "id", mod_id, "is already taken, skipped")
                continue
            taken.add(mod_id)
            extra = {
                "repo": repo["html_url"],
                "stars": repo["stargazers_count"],
                "downloads": sum(downloads_of(a) for a in map(kmod_asset, releases) if a),  # all releases, not only the listed ones
                "updated": versions[0]["published"],
            }
            if package:
                mods.append(mod_entry(package[0], package[1], out, versions, extra))
            else:
                entry = {k: previous[k] for k in ("id", "name", "author", "description", "icon", "cover") if k in previous}
                mods.append(dict(entry, versions=versions, **extra))
        except Exception as e:  # one broken repository must not stop the index
            log(full, e)
    return mods


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="folder with .kmod files instead of GitHub")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, "index.json")
    old = load_json(target, {})
    mods = from_folder(args.local, args.out) if args.local else from_github(args.out, old)
    blocked = load_json("blocklist.json", {})  # {"ids": [], "repos": ["owner/name"]}
    verified = load_json("verified.json", [])  # ["owner/name", or a mod id for local indexes]
    kept = []
    for m in mods:
        full = m.get("repo", "").replace("https://github.com/", "")
        if m["id"] in blocked.get("ids", []) or (full and full in blocked.get("repos", [])):
            continue
        m["verified"] = (full or m["id"]) in verified
        kept.append(m)
    kept.sort(key=lambda m: m["id"])
    if old.get("mods") == kept:
        log("no changes")
        return
    index = {"schema": 1, "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "blocked": blocked.get("ids", []), "mods": kept}
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
        f.write("\n")
    log(f"{len(kept)} mod(s) -> {target}")


if __name__ == "__main__":
    main()
