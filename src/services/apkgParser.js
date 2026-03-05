import JSZip from 'jszip';
import initSqlJs from 'sql.js';
import sqlWasm from 'sql.js/dist/sql-wasm.wasm?url';
import { decompress as fzstdDecompress } from 'fzstd';

// ---------------------------------------------------------------------------
// Protobuf helpers (Anki 2.1.50+ / schema-18 .apkg files)
// ---------------------------------------------------------------------------

function decodeVarint(data, pos) {
  let result = 0;
  let shift = 0;
  while (pos < data.length) {
    const b = data[pos++];
    result |= (b & 0x7f) << shift;
    if (!(b & 0x80)) return [result, pos];
    shift += 7;
  }
  return [result, pos];
}

function extractPbString(data, fieldNumber) {
  let pos = 0;
  while (pos < data.length) {
    let tag, length;
    [tag, pos] = decodeVarint(data, pos);
    const wireType = tag & 0x07;
    const fnum = tag >> 3;
    if (wireType === 0) {
      [, pos] = decodeVarint(data, pos);
    } else if (wireType === 1) {
      pos += 8;
    } else if (wireType === 2) {
      [length, pos] = decodeVarint(data, pos);
      if (fnum === fieldNumber) {
        return new TextDecoder().decode(data.slice(pos, pos + length));
      }
      pos += length;
    } else if (wireType === 5) {
      pos += 4;
    } else {
      break;
    }
  }
  return '';
}

function parsePbMediaEntries(data) {
  const entries = {};
  let pos = 0;
  let entryIdx = 0;
  while (pos < data.length) {
    let tag, length;
    [tag, pos] = decodeVarint(data, pos);
    const wireType = tag & 0x07;
    const fnum = tag >> 3;
    if (wireType === 2) {
      [length, pos] = decodeVarint(data, pos);
      const subData = data.slice(pos, pos + length);
      pos += length;
      if (fnum === 1) {
        // Parse MediaEntry sub-message: field 1 (string) = filename
        let spos = 0;
        let filename = null;
        while (spos < subData.length) {
          let stag, slen;
          [stag, spos] = decodeVarint(subData, spos);
          const swire = stag & 0x07;
          const sfnum = stag >> 3;
          if (swire === 0) {
            [, spos] = decodeVarint(subData, spos);
          } else if (swire === 2) {
            [slen, spos] = decodeVarint(subData, spos);
            const sval = subData.slice(spos, spos + slen);
            spos += slen;
            if (sfnum === 1) {
              filename = new TextDecoder().decode(sval);
            }
          } else if (swire === 1) {
            spos += 8;
          } else if (swire === 5) {
            spos += 4;
          } else {
            break;
          }
        }
        if (filename) {
          entries[String(entryIdx)] = filename;
          entryIdx++;
        }
      }
    } else if (wireType === 0) {
      [, pos] = decodeVarint(data, pos);
    } else if (wireType === 1) {
      pos += 8;
    } else if (wireType === 5) {
      pos += 4;
    } else {
      break;
    }
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Zstd decompression helper
// ---------------------------------------------------------------------------

const ZSTD_MAGIC = [0x28, 0xb5, 0x2f, 0xfd];

function zstdDecompress(data) {
  if (
    data.length >= 4 &&
    data[0] === ZSTD_MAGIC[0] &&
    data[1] === ZSTD_MAGIC[1] &&
    data[2] === ZSTD_MAGIC[2] &&
    data[3] === ZSTD_MAGIC[3]
  ) {
    return fzstdDecompress(data);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Cloze deletion helpers
// ---------------------------------------------------------------------------

function renderCloze(fieldHtml, activeOrd, side) {
  // Build a fresh regex each call to avoid lastIndex state issues
  const re = /\{\{c(\d+)::((?:[^}]|\}(?!\}))*?)(?:::((?:[^}]|\}(?!\}))*?))?\}\}/gs;
  return fieldHtml.replace(re, (_match, cnum, answer, hint) => {
    const num = parseInt(cnum, 10);
    if (num === activeOrd) {
      if (side === 'front') {
        return `<span class="cloze">[${hint || '...'}]</span>`;
      }
      return `<span class="cloze">${answer}</span>`;
    }
    return answer;
  });
}

// ---------------------------------------------------------------------------
// Media embedding
// ---------------------------------------------------------------------------

function embedMedia(html, mediaB64Map) {
  html = html.replace(/src=["']([^"']+)["']/gi, (_match, fname) => {
    const dataUri = mediaB64Map[fname];
    return dataUri ? `src="${dataUri}"` : _match;
  });
  html = html.replace(/\[sound:([^\]]+)\]/g, (_match, fname) => {
    const dataUri = mediaB64Map[fname];
    return dataUri ? `<audio controls src="${dataUri}"></audio>` : '';
  });
  return html;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml',
  mp3: 'audio/mpeg', ogg: 'audio/ogg', wav: 'audio/wav',
  mp4: 'video/mp4', webm: 'video/webm',
};

function guessMime(filename) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  return MIME_MAP[ext] || 'application/octet-stream';
}

function uint8ArrayToBase64(bytes) {
  // Use chunks to avoid call-stack overflow on large arrays
  const CHUNK = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

// ---------------------------------------------------------------------------
// sql.js singleton
// ---------------------------------------------------------------------------

let sqlJsInstance = null;

async function getSqlJs() {
  if (!sqlJsInstance) {
    sqlJsInstance = await initSqlJs({ locateFile: () => sqlWasm });
  }
  return sqlJsInstance;
}

// ---------------------------------------------------------------------------
// Main APKG parser (client-side, no server upload required)
// ---------------------------------------------------------------------------

/**
 * Parse an .apkg (or .colpkg) file entirely in the browser.
 * Mirrors the server-side Python implementation in api/index.py.
 *
 * @param {File} file - The .apkg File object selected by the user
 * @returns {Promise<{title: string, cards: Array<{front: string, back: string, css?: string}>}>}
 */
export async function parseApkg(file) {
  const SQL = await getSqlJs();

  const arrayBuffer = await file.arrayBuffer();
  const zip = await JSZip.loadAsync(arrayBuffer);

  // 1. Locate the SQLite database inside the zip
  let dbName = null;
  for (const candidate of ['collection.anki21b', 'collection.anki21', 'collection.anki2']) {
    if (zip.files[candidate]) {
      dbName = candidate;
      break;
    }
  }
  if (!dbName) {
    throw new Error('Invalid APKG: no collection database found');
  }

  let dbData = await zip.files[dbName].async('uint8array');
  dbData = zstdDecompress(dbData);

  // 2. Build media map: original-filename → base64 data-URI
  const mediaB64Map = {};
  const mediaFile = zip.files['media'];
  if (mediaFile) {
    try {
      let rawMedia = await mediaFile.async('uint8array');
      rawMedia = zstdDecompress(rawMedia);

      let mediaMapFwd = {}; // filename → zip-entry-id
      try {
        // Legacy format: JSON  { "0": "image.jpg", ... }
        const mediaJson = JSON.parse(new TextDecoder().decode(rawMedia));
        for (const [id, fname] of Object.entries(mediaJson)) {
          mediaMapFwd[fname] = id;
        }
      } catch {
        // New format: protobuf MediaEntries
        const pbEntries = parsePbMediaEntries(rawMedia);
        for (const [id, fname] of Object.entries(pbEntries)) {
          mediaMapFwd[fname] = id;
        }
      }

      for (const [fname, entryId] of Object.entries(mediaMapFwd)) {
        const entry = zip.files[entryId];
        if (!entry) continue;
        try {
          let data = await entry.async('uint8array');
          data = zstdDecompress(data);
          const mime = guessMime(fname);
          mediaB64Map[fname] = `data:${mime};base64,${uint8ArrayToBase64(data)}`;
        } catch {
          // Skip unreadable media
        }
      }
    } catch {
      // No usable media map — text-only parsing continues
    }
  }

  // 3. Open SQLite
  const db = new SQL.Database(dbData);
  let title = file.name.replace(/\.(apkg|colpkg)$/i, '');
  let models = {};

  // Detect schema: new (notetypes table) vs legacy (col.models JSON)
  const tableRes = db.exec(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='notetypes'"
  );
  const isNewSchema = tableRes.length > 0 && tableRes[0].values.length > 0;

  if (isNewSchema) {
    // New schema (Anki 2.1.50+ / schema 18+)
    const ntRes = db.exec('SELECT id, name, config FROM notetypes');
    for (const [ntId, ntName, ntConfig] of (ntRes[0]?.values ?? [])) {
      const ntCfg = ntConfig ? new Uint8Array(ntConfig) : new Uint8Array();
      const css = ntCfg.length ? extractPbString(ntCfg, 3) : '';

      const fieldsRes = db.exec(
        `SELECT name, ord FROM fields WHERE ntid=${ntId} ORDER BY ord`
      );
      const flds = (fieldsRes[0]?.values ?? []).map(([name, ord]) => ({ name, ord }));

      const tmplsRes = db.exec(
        `SELECT name, ord, config FROM templates WHERE ntid=${ntId} ORDER BY ord`
      );
      const tmpls = (tmplsRes[0]?.values ?? []).map(([name, , config]) => {
        const tCfg = config ? new Uint8Array(config) : new Uint8Array();
        return {
          name,
          qfmt: tCfg.length ? extractPbString(tCfg, 1) : '',
          afmt: tCfg.length ? extractPbString(tCfg, 2) : '',
        };
      });

      models[String(ntId)] = { name: ntName, css, flds, tmpls };
    }

    // Deck title from the new `decks` table
    try {
      const deckRes = db.exec('SELECT id, name FROM decks WHERE id != 1 LIMIT 1');
      if (deckRes[0]?.values?.[0]) {
        title = deckRes[0].values[0][1];
      }
    } catch {
      // Keep file-name title
    }
  } else {
    // Legacy schema (Anki < 2.1.50)
    const colRes = db.exec('SELECT models, decks FROM col');
    if (!colRes[0]?.values?.[0]) {
      db.close();
      throw new Error('Invalid Anki DB: empty col table');
    }
    const [modelsJson, decksJson] = colRes[0].values[0];
    models = JSON.parse(modelsJson);

    try {
      const decks = JSON.parse(decksJson);
      for (const dk of Object.values(decks)) {
        if (String(dk.id) !== '1') {
          title = dk.name || title;
          break;
        }
      }
    } catch {
      // Keep file-name title
    }
  }

  // 4. Fetch all notes
  const notesRes = db.exec('SELECT id, mid, flds, tags FROM notes');
  const notes = notesRes[0]?.values ?? [];

  // 5. Build note-id → card-ords map
  const noteCardOrds = {};
  try {
    const cardsRes = db.exec('SELECT nid, ord FROM cards ORDER BY nid, ord');
    for (const [nid, ord] of (cardsRes[0]?.values ?? [])) {
      if (!noteCardOrds[nid]) noteCardOrds[nid] = [];
      noteCardOrds[nid].push(ord);
    }
  } catch {
    // cards table might not exist in very old exports
  }

  // 6. Generate card objects
  const cards = [];

  for (const [noteId, mid, fldsStr, tags] of notes) {
    const model = models[String(mid)];
    if (!model) continue;

    const fieldValues = fldsStr.split('\x1f');
    const fieldDefs = model.flds || [];
    const css = model.css || '';
    const tagsStr = (tags || '').trim();

    const fieldsDict = {};
    for (let i = 0; i < fieldDefs.length; i++) {
      const fname = fieldDefs[i]?.name ?? `Field${i}`;
      fieldsDict[fname] = fieldValues[i] ?? '';
    }

    const tmpls = model.tmpls || [];
    if (!tmpls.length) {
      if (fieldValues.length >= 2) {
        cards.push({
          front: embedMedia(fieldValues[0], mediaB64Map),
          back: embedMedia(fieldValues[1], mediaB64Map),
          css,
        });
      }
      continue;
    }

    const isCloze = tmpls.some(
      t => (t.qfmt || '').includes('{{cloze:') || (t.afmt || '').includes('{{cloze:')
    );

    if (isCloze) {
      // Cloze note type
      const tmpl = tmpls[0];
      const qfmt = tmpl.qfmt || '';
      const afmt = tmpl.afmt || '';

      let cardOrds = noteCardOrds[noteId] || [];
      if (!cardOrds.length) {
        const allText = fieldValues.join('\x1f');
        const clozeNums = new Set(
          [...allText.matchAll(/\{\{c(\d+)::/g)].map(m => parseInt(m[1], 10))
        );
        const sorted = [...clozeNums].sort((a, b) => a - b);
        cardOrds = sorted.length ? sorted.map(n => n - 1) : [0];
      }

      for (const cardOrd of cardOrds) {
        const clozeNum = cardOrd + 1;

        const clozeFieldsFront = {};
        const clozeFieldsBack = {};
        for (const [fname, fval] of Object.entries(fieldsDict)) {
          clozeFieldsFront[fname] = renderCloze(fval, clozeNum, 'front');
          clozeFieldsBack[fname] = renderCloze(fval, clozeNum, 'back');
        }

        let frontHtml = qfmt;
        let backHtml = afmt;

        for (const fname of Object.keys(fieldsDict)) {
          frontHtml = frontHtml.replaceAll(`{{cloze:${fname}}}`, clozeFieldsFront[fname]);
          backHtml = backHtml.replaceAll(`{{cloze:${fname}}}`, clozeFieldsBack[fname]);
        }
        for (const [fname, fval] of Object.entries(fieldsDict)) {
          frontHtml = frontHtml.replaceAll(`{{${fname}}}`, fval);
          backHtml = backHtml.replaceAll(`{{${fname}}}`, fval);
        }
        frontHtml = frontHtml.replaceAll('{{Tags}}', tagsStr);
        backHtml = backHtml.replaceAll('{{Tags}}', tagsStr);
        backHtml = backHtml.replaceAll('{{FrontSide}}', frontHtml);
        for (const [fname, fval] of Object.entries(fieldsDict)) {
          frontHtml = frontHtml.replaceAll(`{{type:${fname}}}`, fval);
          backHtml = backHtml.replaceAll(`{{type:${fname}}}`, fval);
        }
        frontHtml = frontHtml.replace(/\{\{[^}]*\}\}/g, '');
        backHtml = backHtml.replace(/\{\{[^}]*\}\}/g, '');

        cards.push({
          front: embedMedia(frontHtml, mediaB64Map),
          back: embedMedia(backHtml, mediaB64Map),
          css,
        });
      }
    } else {
      // Standard note type
      const cardOrds = noteCardOrds[noteId] || [0];
      for (const cardOrd of cardOrds) {
        const tmpl = cardOrd < tmpls.length ? tmpls[cardOrd] : tmpls[0];
        let frontHtml = tmpl.qfmt || '';
        let backHtml = tmpl.afmt || '';

        for (const [fname, fval] of Object.entries(fieldsDict)) {
          frontHtml = frontHtml.replaceAll(`{{${fname}}}`, fval);
          backHtml = backHtml.replaceAll(`{{${fname}}}`, fval);
        }
        frontHtml = frontHtml.replaceAll('{{Tags}}', tagsStr);
        backHtml = backHtml.replaceAll('{{Tags}}', tagsStr);
        backHtml = backHtml.replaceAll('{{FrontSide}}', frontHtml);
        for (const [fname, fval] of Object.entries(fieldsDict)) {
          frontHtml = frontHtml.replaceAll(`{{type:${fname}}}`, fval);
          backHtml = backHtml.replaceAll(`{{type:${fname}}}`, fval);
        }
        frontHtml = frontHtml.replace(/\{\{[^}]*\}\}/g, '');
        backHtml = backHtml.replace(/\{\{[^}]*\}\}/g, '');

        cards.push({
          front: embedMedia(frontHtml, mediaB64Map),
          back: embedMedia(backHtml, mediaB64Map),
          css,
        });
      }
    }
  }

  db.close();
  return { title, cards };
}
