// Paste into: your Google Sheet -> Extensions -> Apps Script. Then Deploy -> New deployment ->
// type "Web app", Execute as "Me", Who has access "Anyone". Copy the web app URL into .env.
// Change SECRET to a long random string and put the SAME value in .env (SHEETS_SECRET).
const SECRET = 'CHANGE_ME_TO_A_LONG_RANDOM_STRING';

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.secret !== SECRET) return ContentService.createTextOutput('forbidden');
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sh = ss.getSheetByName(d.tab) || ss.insertSheet(d.tab);
    if (sh.getLastRow() === 0 && d.header) {
      sh.appendRow(d.header);
      sh.setFrozenRows(1);
      sh.getRange(1, 1, 1, d.header.length).setFontWeight('bold');
    }
    if (d.rows && d.rows.length) {
      const rows = d.rows.map(r => r.map(v => (typeof v === 'string' && /^\d{4}-\d\d-\d\dT/.test(v)) ? v.replace('T', ' ').slice(0, 19) : v));
      sh.getRange(sh.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
    }
    return ContentService.createTextOutput('ok');
  } finally {
    lock.release();
  }
}
