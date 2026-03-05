from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests as cffi_requests
import zipfile
import sqlite3
import os
import tempfile
import json
import re
import base64
import mimetypes
import shutil
import urllib.parse
from pathlib import Path
import zstandard as zstd

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Protobuf helpers  (for Anki 2.1.50+ / schema-18 .apkg files)
# ---------------------------------------------------------------------------

def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint starting at *pos*. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _extract_pb_string(data: bytes, field_number: int) -> str:
    """Extract a *string* field from a serialized protobuf message.

    Only handles wire-types 0 (varint), 1 (64-bit), 2 (length-delimited)
    and 5 (32-bit) which is enough for the Anki config blobs we care about.

    Field numbers we use:
      - CardTemplateConfig : 1 = q_format, 2 = a_format
      - NotetypeConfig     : 3 = css
    """
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        wire_type = tag & 0x07
        fnum = tag >> 3

        if wire_type == 0:          # varint
            _, pos = _decode_varint(data, pos)
        elif wire_type == 1:        # 64-bit fixed
            pos += 8
        elif wire_type == 2:        # length-delimited (string / bytes / sub-msg)
            length, pos = _decode_varint(data, pos)
            if fnum == field_number:
                return data[pos:pos + length].decode('utf-8', errors='replace')
            pos += length
        elif wire_type == 5:        # 32-bit fixed
            pos += 4
        else:
            break               # unknown wire type – stop
    return ''


# ---------------------------------------------------------------------------
# Media-map helpers  (for Anki 2.1.50+ protobuf media index)
# ---------------------------------------------------------------------------

def _parse_pb_media_entries(data: bytes) -> dict:
    """Parse a *MediaEntries* protobuf blob into {zip-entry-id: filename}.

    The outer message has repeated field 1 (MediaEntry sub-messages).
    Each MediaEntry has:
      field 1 (string): filename
      field 2 (uint64): size
      field 3 (bytes) : sha1 checksum

    Returns a dict like {"0": "image.jpg", "1": "audio.mp3", ...}.
    """
    entries = {}
    pos = 0
    entry_idx = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        wire_type = tag & 0x07
        fnum = tag >> 3
        if wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            sub_data = data[pos:pos + length]
            pos += length
            if fnum == 1:
                # Parse sub-message (MediaEntry)
                spos = 0
                filename = None
                while spos < len(sub_data):
                    stag, spos = _decode_varint(sub_data, spos)
                    swire = stag & 0x07
                    sfnum = stag >> 3
                    if swire == 0:
                        _, spos = _decode_varint(sub_data, spos)
                    elif swire == 2:
                        slen, spos = _decode_varint(sub_data, spos)
                        sval = sub_data[spos:spos + slen]
                        spos += slen
                        if sfnum == 1:
                            filename = sval.decode('utf-8', errors='replace')
                    elif swire == 1:
                        spos += 8
                    elif swire == 5:
                        spos += 4
                    else:
                        break
                if filename:
                    entries[str(entry_idx)] = filename
                    entry_idx += 1
        elif wire_type == 0:
            _, pos = _decode_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            break
    return entries


def _zstd_decompress(data: bytes) -> bytes:
    """Decompress *data* if it starts with the zstd magic number."""
    if data[:4] == b'\x28\xb5\x2f\xfd':
        dctx = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(data)
        data = reader.read()
        reader.close()
    return data


# ---------------------------------------------------------------------------
# Cloze-deletion helpers
# ---------------------------------------------------------------------------

# Matches {{c1::answer}}, {{c1::answer::hint}}, and nested HTML inside
_CLOZE_RE = re.compile(
    r'\{\{c(\d+)::((?:[^}]|\}(?!\}))*?)'   # {{cN::content
    r'(?:::((?:[^}]|\}(?!\}))*?))?'          # optional ::hint
    r'\}\}',                                  # closing }}
    re.DOTALL,
)


def _render_cloze(field_html: str, active_ord: int, side: str = 'front') -> str:
    """Process cloze deletions in *field_html*.

    For the *active_ord* cloze (the one this card tests):
      - front: replace with [...] (or [hint] if provided)
      - back:  wrap answer in <span class="cloze">...</span>

    For all OTHER cloze ordinals, show the answer text as-is (no brackets).
    """
    def _replace(m):
        cnum = int(m.group(1))
        answer = m.group(2)
        hint = m.group(3)  # may be None

        if cnum == active_ord:
            if side == 'front':
                return f'<span class="cloze">[{hint if hint else "..."}]</span>'
            else:
                return f'<span class="cloze">{answer}</span>'
        else:
            # Not the active cloze — show the answer as normal text
            return answer

    return _CLOZE_RE.sub(_replace, field_html)


# ---------------------------------------------------------------------------
# Quizlet helpers
# ---------------------------------------------------------------------------

def _extract_cards_from_html(html: str) -> list[dict]:
    """Try multiple strategies to pull term/definition pairs from Quizlet HTML."""
    cards = []

    # Strategy 1: __NEXT_DATA__ JSON → studiableItems[].cardSides[]
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            nd = json.loads(m.group(1))
            props = nd.get("props", {}).get("pageProps", {})
            dehydrated = props.get("dehydratedReduxStateKey")
            if isinstance(dehydrated, str):
                dehydrated = json.loads(dehydrated)
            if isinstance(dehydrated, dict):
                study_data = dehydrated.get("studyModesCommon", {}).get("studiableData", {})
                items = study_data.get("studiableItems", [])
                for item in items:
                    if item.get("isDeleted"):
                        continue
                    card_sides = item.get("cardSides", [])
                    front = ""
                    back = ""
                    for side in card_sides:
                        label = side.get("label", "")
                        media = side.get("media", [])
                        text = media[0].get("plainText", "") if media else ""
                        if label == "word":
                            front = text
                        elif label == "definition":
                            back = text
                    if front or back:
                        cards.append({"front": front, "back": back})
                if cards:
                    return cards
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Strategy 2: window.Quizlet["setPageData"] (older pages)
    m2 = re.search(r'window\.Quizlet\["setPageData"\]\s*=\s*({.+?});\s*</script>', html, re.DOTALL)
    if m2:
        try:
            blob = json.loads(m2.group(1))
            term_map = blob.get("termIdToTermsMap", {})
            for _id, item in term_map.items():
                word = (item.get("wordSide") or {}).get("media", [{}])
                defn = (item.get("definitionSide") or {}).get("media", [{}])
                front = word[0].get("plainText", "") if word else ""
                back  = defn[0].get("plainText", "") if defn else ""
                if front or back:
                    cards.append({"front": front, "back": back})
            if cards:
                return cards
        except (json.JSONDecodeError, KeyError):
            pass

    # Strategy 3: brute-force plainText regex (absolute fallback)
    texts = re.findall(r'"plainText"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if len(texts) >= 2:
        cleaned = [t.encode().decode("unicode_escape", errors="replace") for t in texts]
        for i in range(0, len(cleaned) - 1, 2):
            cards.append({"front": cleaned[i], "back": cleaned[i + 1]})

    return cards


@app.route('/api/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"})


def _fetch_quizlet_html(url: str) -> tuple[int, str]:
    """Fetch the HTML for a Quizlet URL, routing through a proxy/ScraperAPI when configured.

    On Vercel (or any datacenter host) Quizlet's Cloudflare protection blocks
    requests originating from datacenter IP ranges.  Two escape hatches are
    supported via environment variables:

    SCRAPER_API_KEY  – API key for scraperapi.com (free tier: 5,000 req/month).
                       When set, the request is routed through ScraperAPI which
                       uses residential IPs to bypass Cloudflare.
                       Sign up free at https://www.scraperapi.com/

    QUIZLET_PROXY_URL – A full proxy URL (http/https/socks5) that will be
                        passed directly to curl_cffi.  Useful when you supply
                        your own residential or rotating proxy.
                        Example: "socks5://user:pass@proxy.example.com:1080"

    If neither variable is set the request falls back to a direct curl_cffi
    Chrome-impersonation call (works fine on local / non-datacenter hosts).
    """
    # Validate that the URL targets quizlet.com to prevent SSRF
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not (
        parsed.netloc == 'quizlet.com' or parsed.netloc.endswith('.quizlet.com')
    ):
        raise ValueError("URL must be a quizlet.com address")

    scraper_api_key = os.environ.get('SCRAPER_API_KEY', '').strip()
    proxy_url = os.environ.get('QUIZLET_PROXY_URL', '').strip()

    if scraper_api_key:
        # Route through ScraperAPI – it handles JS rendering and Cloudflare bypass
        api_url = (
            'http://api.scraperapi.com'
            f'?api_key={scraper_api_key}'
            f'&url={urllib.parse.quote_plus(url)}'
            '&render=true'
        )
        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=60)
    elif proxy_url:
        # Use caller-supplied proxy with Chrome TLS fingerprint
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            proxies={"https": proxy_url, "http": proxy_url},
            timeout=30,
        )
    else:
        # Direct connection — works locally, may be blocked on Vercel
        resp = cffi_requests.get(url, impersonate="chrome", timeout=15)

    return resp.status_code, resp.text


@app.route('/api/quizlet', methods=['POST'])
def fetch_quizlet():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        status_code, html = _fetch_quizlet_html(url)

        if status_code != 200:
            return jsonify({"error": f"Quizlet returned status {status_code}"}), status_code

        # Check for captcha / block page
        if "px-captcha" in html or "Access to this page has been denied" in html:
            return jsonify({
                "error": (
                    "Quizlet blocked the request. "
                    "Set SCRAPER_API_KEY or QUIZLET_PROXY_URL in your Vercel environment "
                    "to route through a residential IP. "
                    "See https://www.scraperapi.com/ for a free tier."
                )
            }), 403

        cards = _extract_cards_from_html(html)
        if not cards:
            return jsonify({"error": "Could not find flashcard data on this page. Make sure the URL is a valid Quizlet set."}), 422

        return jsonify({"cards": cards, "count": len(cards)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/apkg', methods=['POST'])
def parse_apkg():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        cards = []
        title = file.filename.replace('.apkg', '').replace('.colpkg', '')

        tmpdir = tempfile.mkdtemp()
        try:
            zip_path = os.path.join(tmpdir, "deck.apkg")
            file.save(zip_path)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                namelist = zf.namelist()

                # ----- 1. Find the SQLite database --------------------------
                db_name = None
                for candidate in ('collection.anki21b', 'collection.anki21', 'collection.anki2'):
                    if candidate in namelist:
                        db_name = candidate
                        break
                if db_name is None:
                    return jsonify({"error": "Invalid APKG: no collection database found"}), 400

                db_path = os.path.join(tmpdir, db_name)
                raw_db = zf.read(db_name)

                # Anki 2.1.50+ may zstd-compress the database.
                raw_db = _zstd_decompress(raw_db)

                with open(db_path, 'wb') as f:
                    f.write(raw_db)

                # ----- 2. Build media map -----------------------------------
                #  media maps  zip-entry-id -> original-filename
                #  Legacy (JSON):   {"0": "image.jpg", ...}
                #  New (protobuf):  binary blob, possibly zstd-compressed
                media_map_fwd = {}   # "image.jpg" -> "0"
                if 'media' in namelist:
                    try:
                        raw_media = zf.read('media')
                        raw_media = _zstd_decompress(raw_media)
                        # Try JSON first (legacy format)
                        try:
                            media_json = json.loads(
                                raw_media.decode('utf-8'))
                            media_map_fwd = {
                                v: k for k, v in media_json.items()}
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # Fall back to protobuf (new format)
                            pb_entries = _parse_pb_media_entries(raw_media)
                            media_map_fwd = {
                                v: k for k, v in pb_entries.items()}
                    except Exception:
                        pass  # no usable media map – still parse text

                def _read_media_b64(filename: str) -> str | None:
                    """Return a data-URI for *filename* or None."""
                    entry_id = media_map_fwd.get(filename)
                    if entry_id is None:
                        return None
                    try:
                        data = zf.read(entry_id)
                        # Media files may also be zstd-compressed
                        data = _zstd_decompress(data)
                        mime = (mimetypes.guess_type(filename)[0]
                                or 'application/octet-stream')
                        b64 = base64.b64encode(data).decode('ascii')
                        return f"data:{mime};base64,{b64}"
                    except Exception:
                        return None

                def _embed_media(html: str) -> str:
                    """Replace src="filename" and [sound:filename] with base64 data URIs."""
                    def _replace_src(m):
                        fname = m.group(1)
                        uri = _read_media_b64(fname)
                        if uri:
                            return f'src="{uri}"'
                        return m.group(0)

                    html = re.sub(r'src=["\']([^"\']+)["\']', _replace_src, html, flags=re.IGNORECASE)
                    # [sound:file.mp3] → <audio> tag
                    def _replace_sound(m):
                        fname = m.group(1)
                        uri = _read_media_b64(fname)
                        if uri:
                            return f'<audio controls src="{uri}"></audio>'
                        return ''
                    html = re.sub(r'\[sound:([^\]]+)\]', _replace_sound, html)
                    return html

                # ----- 3. Open SQLite and read models + notes ---------------
                # Open in immutable mode so SQLite won't create WAL/SHM
                # lock files that block temp-dir cleanup on Windows.
                try:
                    db_uri = Path(db_path).as_uri() + "?immutable=1"
                    conn = sqlite3.connect(db_uri, uri=True)
                except Exception:
                    conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Detect schema version: Anki 2.1.50+ uses a `notetypes` table
                # instead of the legacy `col.models` JSON column.
                cur.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='notetypes'"
                )
                is_new_schema = cur.fetchone() is not None

                if is_new_schema:
                    # ── New schema (Anki 2.1.50+ / schema 18+) ──────────
                    models = {}
                    cur.execute("SELECT id, name, config FROM notetypes")
                    for nt in cur.fetchall():
                        nt_id = str(nt['id'])
                        nt_cfg = bytes(nt['config']) if nt['config'] else b''
                        css = _extract_pb_string(nt_cfg, 3) if nt_cfg else ''

                        cur.execute(
                            "SELECT name, ord FROM fields "
                            "WHERE ntid=? ORDER BY ord", (nt['id'],)
                        )
                        flds = [{'name': f['name'], 'ord': f['ord']}
                                for f in cur.fetchall()]

                        cur.execute(
                            "SELECT name, ord, config FROM templates "
                            "WHERE ntid=? ORDER BY ord", (nt['id'],)
                        )
                        tmpls = []
                        for t in cur.fetchall():
                            t_cfg = bytes(t['config']) if t['config'] else b''
                            qfmt = _extract_pb_string(t_cfg, 1) if t_cfg else ''
                            afmt = _extract_pb_string(t_cfg, 2) if t_cfg else ''
                            tmpls.append({
                                'name': t['name'],
                                'qfmt': qfmt,
                                'afmt': afmt,
                            })

                        models[nt_id] = {
                            'name': nt['name'],
                            'css': css,
                            'flds': flds,
                            'tmpls': tmpls,
                        }

                    # Deck title from the new `decks` table
                    try:
                        cur.execute(
                            "SELECT id, name FROM decks "
                            "WHERE id != 1 LIMIT 1"
                        )
                        dk_row = cur.fetchone()
                        if dk_row:
                            title = dk_row['name']
                    except Exception:
                        pass

                else:
                    # ── Legacy schema (Anki < 2.1.50) ──────────────────
                    cur.execute("SELECT models, decks FROM col")
                    col_row = cur.fetchone()
                    if not col_row:
                        conn.close()
                        return jsonify({"error": "Invalid Anki DB: empty col table"}), 400

                    models = json.loads(col_row['models'])
                    decks_json = json.loads(col_row['decks'])

                    # Derive a nicer title from the first non-Default deck
                    try:
                        for dk in decks_json.values():
                            if str(dk.get('id')) != '1':
                                title = dk.get('name', title)
                                break
                    except Exception:
                        pass

                # Notes (same structure in both schemas)
                cur.execute("SELECT id, mid, flds, tags FROM notes")
                notes = cur.fetchall()

                # Build note-id → list-of-card-ords from the cards table
                # so we know which cloze numbers / template ords to render.
                note_card_ords = {}
                try:
                    cur.execute("SELECT nid, ord FROM cards ORDER BY nid, ord")
                    for row in cur.fetchall():
                        note_card_ords.setdefault(row['nid'], []).append(row['ord'])
                except Exception:
                    pass  # cards table might not exist in very old exports

                for note in notes:
                    mid = str(note['mid'])
                    model = models.get(mid)
                    if not model:
                        continue

                    field_values = note['flds'].split('\x1f')
                    field_defs = model.get('flds', [])
                    css = model.get('css', '')
                    tags = note['tags'].strip() if note['tags'] else ''

                    # Map field-name → value
                    fields_dict = {}
                    for i, fdef in enumerate(field_defs):
                        fname = fdef.get('name', f'Field{i}')
                        fields_dict[fname] = field_values[i] if i < len(field_values) else ''

                    tmpls = model.get('tmpls', [])
                    if not tmpls:
                        # Fallback: raw first two fields
                        if len(field_values) >= 2:
                            cards.append({
                                "front": _embed_media(field_values[0]),
                                "back": _embed_media(field_values[1]),
                                "css": css,
                            })
                        continue

                    # Detect if this is a cloze model by checking templates
                    is_cloze = any('{{cloze:' in t.get('qfmt', '') or
                                   '{{cloze:' in t.get('afmt', '')
                                   for t in tmpls)

                    if is_cloze:
                        # ── Cloze note type ─────────────────────────────
                        # Cloze models always use the first (and only) template.
                        # Each card from this note uses a different cloze ordinal.
                        tmpl = tmpls[0]
                        qfmt = tmpl.get('qfmt', '')
                        afmt = tmpl.get('afmt', '')

                        # Figure out which cloze ordinals exist
                        card_ords = note_card_ords.get(note['id'], [])
                        if not card_ords:
                            # Fallback: scan the fields for {{cN::...}}
                            all_text = '\x1f'.join(field_values)
                            cloze_nums = set(int(x) for x in
                                             re.findall(r'\{\{c(\d+)::', all_text))
                            card_ords = sorted(cloze_nums) if cloze_nums else [1]
                            # Convert to 0-based
                            card_ords = [n - 1 for n in card_ords]

                        for card_ord in card_ords:
                            cloze_num = card_ord + 1  # 0-based ord → 1-based cN

                            # Process each cloze field
                            cloze_fields_front = {}
                            cloze_fields_back = {}
                            for fname, fval in fields_dict.items():
                                cloze_fields_front[fname] = _render_cloze(
                                    fval, cloze_num, side='front')
                                cloze_fields_back[fname] = _render_cloze(
                                    fval, cloze_num, side='back')

                            front_html = qfmt
                            back_html = afmt

                            # Substitute {{cloze:FieldName}}
                            for fname in fields_dict:
                                front_html = front_html.replace(
                                    '{{cloze:' + fname + '}}',
                                    cloze_fields_front[fname])
                                back_html = back_html.replace(
                                    '{{cloze:' + fname + '}}',
                                    cloze_fields_back[fname])

                            # Substitute plain {{FieldName}} and {{Tags}}
                            for fname, fval in fields_dict.items():
                                front_html = front_html.replace(
                                    '{{' + fname + '}}', fval)
                                back_html = back_html.replace(
                                    '{{' + fname + '}}', fval)
                            front_html = front_html.replace('{{Tags}}', tags)
                            back_html = back_html.replace('{{Tags}}', tags)

                            # {{FrontSide}} on the back
                            back_html = back_html.replace(
                                '{{FrontSide}}', front_html)

                            # type: fields
                            for fname, fval in fields_dict.items():
                                front_html = front_html.replace(
                                    '{{type:' + fname + '}}', fval)
                                back_html = back_html.replace(
                                    '{{type:' + fname + '}}', fval)

                            # Strip leftover
                            front_html = re.sub(r'\{\{[^}]*\}\}', '', front_html)
                            back_html = re.sub(r'\{\{[^}]*\}\}', '', back_html)

                            front_html = _embed_media(front_html)
                            back_html = _embed_media(back_html)

                            cards.append({
                                "front": front_html,
                                "back": back_html,
                                "css": css,
                            })
                    else:
                        # ── Standard note type ──────────────────────────
                        # Use the cards table to pick the right template ord,
                        # or fall back to the first template.
                        card_ords = note_card_ords.get(note['id'], [0])
                        for card_ord in card_ords:
                            tmpl = tmpls[card_ord] if card_ord < len(tmpls) else tmpls[0]
                            front_html = tmpl.get('qfmt', '')
                            back_html = tmpl.get('afmt', '')

                            # Substitute {{FieldName}} placeholders
                            for fname, fval in fields_dict.items():
                                front_html = front_html.replace(
                                    '{{' + fname + '}}', fval)
                                back_html = back_html.replace(
                                    '{{' + fname + '}}', fval)

                            front_html = front_html.replace('{{Tags}}', tags)
                            back_html = back_html.replace('{{Tags}}', tags)

                            # Handle {{FrontSide}} in the back template
                            rendered_front = front_html
                            back_html = back_html.replace(
                                '{{FrontSide}}', rendered_front)

                            # Handle {{type:FieldName}}
                            for fname, fval in fields_dict.items():
                                front_html = front_html.replace(
                                    '{{type:' + fname + '}}', fval)
                                back_html = back_html.replace(
                                    '{{type:' + fname + '}}', fval)

                            # Strip leftover unresolved {{...}} tags
                            front_html = re.sub(
                                r'\{\{[^}]*\}\}', '', front_html)
                            back_html = re.sub(
                                r'\{\{[^}]*\}\}', '', back_html)

                            # Embed media (images, audio)
                            front_html = _embed_media(front_html)
                            back_html = _embed_media(back_html)

                            cards.append({
                                "front": front_html,
                                "back": back_html,
                                "css": css,
                            })

                conn.close()

            return jsonify({"title": title, "cards": cards, "count": len(cards)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5328, debug=True)
