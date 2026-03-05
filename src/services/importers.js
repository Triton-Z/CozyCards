import Papa from 'papaparse';
import { parseApkg as parseApkgClient } from './apkgParser.js';

/**
 * Parses a Quizlet set URL to extract term/definition pairs.
 * Calls the Python backend which uses curl_cffi to bypass Cloudflare/PerimeterX.
 * @param {string} url - The Quizlet set URL
 * @returns {Promise<{title: string, cards: Array<{front: string, back: string}>}>}
 */
export async function fetchQuizletSet(url) {
  // Validate URL basic structure
  if (!url.includes('quizlet.com')) {
    throw new Error('Please provide a valid Quizlet URL');
  }

  try {
    const response = await fetch('/api/quizlet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || `Server returned ${response.status}`);
    }

    const cards = data.cards || [];
    if (cards.length === 0) {
      throw new Error('No flashcards found in this set.');
    }

    // Extract a title from the URL slug as a fallback
    const slug = url.split('/').filter(Boolean).pop() || 'Imported Quizlet Deck';
    const title = slug.replace(/-/g, ' ').replace(/flash cards$/i, '').trim() || 'Imported Quizlet Deck';

    return { title, cards };
  } catch (error) {
    console.error('Quizlet Import Error:', error);
    throw error;
  }
}

/**
 * Parses an .apkg file entirely in the browser (no server upload required).
 * Uses JSZip to extract the archive, sql.js to read the SQLite database, and
 * fzstd to handle zstd-compressed Anki 2.1.50+ decks.
 * This avoids Vercel's 4.5 MB body-size limit that blocks large deck uploads.
 * @param {File} file - The .apkg File object
 * @returns {Promise<{title: string, cards: Array<{front: string, back: string, css?: string}>}>}
 */
export async function uploadApkg(file) {
  return parseApkgClient(file);
}

/**
 * Parses a CSV string or Anki text export into flashcards.
 * @param {File} file - The file object
 * @returns {Promise<{title: string, cards: Array<{front: string, back: string}>}>}
 */
export function parseCSV(file) {
  return new Promise((resolve, reject) => {
    Papa.parse(file, {
      complete: (results) => {
        const data = results.data;
        if (!data || data.length === 0) {
          return resolve({ title: file.name.replace(/\.[^/.]+$/, ""), cards: [] });
        }
        
        // Simple heuristic for header detection
        let cards = [];
        let startIndex = 0;
        
        // Check if first row is likely a header
        const firstRow = data[0];
        const isHeader = Array.isArray(firstRow) && firstRow.some(cell => 
            /front|term|question|back|definition|answer/i.test(String(cell))
        );

        if (isHeader) {
            startIndex = 1;
             // Determine indices
             const frontIndex = firstRow.findIndex(c => /front|term|question/i.test(String(c)));
             const backIndex = firstRow.findIndex(c => /back|definition|answer/i.test(String(c)));
             
             // If we found both headers
             if (frontIndex !== -1 && backIndex !== -1) {
                 for (let i = 1; i < data.length; i++) {
                     const row = data[i];
                     if (Array.isArray(row) && row.length > Math.max(frontIndex, backIndex)) {
                         cards.push({
                             front: row[frontIndex],
                             back: row[backIndex]
                         });
                     }
                 }
                 resolve({
                     title: file.name.replace(/\.[^/.]+$/, ""),
                     cards: cards.filter(c => c.front && c.back)
                 });
                 return;
             }
        }

        // Fallback: Assume Col 1 is Front, Col 2 is Back
        cards = data.slice(startIndex).map(row => {
            if (Array.isArray(row) && row.length >= 2) {
                return {
                    front: row[0],
                    back: row[1]
                };
            }
            return null;
        }).filter(c => c && c.front && c.back);

        resolve({
          title: file.name.replace(/\.[^/.]+$/, ""),
          cards: cards
        });
      },
      error: (error) => {
        reject(error);
      },
      header: false, 
      skipEmptyLines: true
    });
  });
}
