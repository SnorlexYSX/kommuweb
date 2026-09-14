/**
 * Forward-only: new PaymentGateway rows → Newsletter (skip if already subscribed).
 *
 * Does NOT backfill historical PaymentGateway rows. On first run it records the
 * current last row and only processes rows appended after that.
 * Does NOT update existing Newsletter rows (no name fill-back).
 *
 * Add as a NEW file in your existing KA Inventory Apps Script project
 * (the one that already has newsletter subscribe + onEdit). Do not paste
 * into Code.gs — File → + → Script, name it e.g. PaymentGatewayNewsletter.
 *
 * Reuses SPREADSHEET_ID, NEWSLETTER_TAB, EMAIL_RE, HEADERS from
 * newsletter-subscribe-api (already in Code.gs).
 *
 * Setup (once):
 *   1. Paste this file as a new script in the same project
 *   2. Run → installPaymentGatewayNewsletterTrigger
 *   3. Approve permissions when prompted
 *
 * Manual run: syncPaymentGatewayToNewsletter
 * Auto: time-driven trigger every 10 minutes
 *
 * New Newsletter rows: source=checkout, sequence_step=0, status=active
 */
var PAYMENT_GATEWAY_TAB = 'PaymentGateway';
var PAYMENT_GATEWAY_SOURCE = 'checkout';
var PG_LAST_ROW_PROP = 'paymentGatewayNewsletterLastRow';
var EMAIL_HEADER_ALIASES = {
  email: true,
  'customer email': true,
  customer_email: true,
  'e-mail': true,
  'buyer email': true,
  'payer email': true
};
var NAME_HEADER_ALIASES = {
  name: true,
  'customer name': true,
  customer_name: true,
  'buyer name': true,
  'full name': true,
  'payer name': true
};

/**
 * Install (or refresh) a 10-minute sync trigger.
 * Deletes prior triggers for syncPaymentGatewayToNewsletter first.
 * Seeds the row cursor so existing PaymentGateway rows are not backfilled.
 */
function installPaymentGatewayNewsletterTrigger() {
  seedPaymentGatewayRowCursor_();
  var handlers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < handlers.length; i++) {
    if (handlers[i].getHandlerFunction() === 'syncPaymentGatewayToNewsletter') {
      ScriptApp.deleteTrigger(handlers[i]);
    }
  }
  ScriptApp.newTrigger('syncPaymentGatewayToNewsletter')
    .timeBased()
    .everyMinutes(10)
    .create();
  Logger.log('Installed: syncPaymentGatewayToNewsletter every 10 minutes (new rows only)');
}

/**
 * Mark current PaymentGateway last row as already seen (no historical sync).
 * Safe to re-run if you want to skip everything currently in the tab.
 */
function seedPaymentGatewayRowCursor_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var paymentSheet = ss.getSheetByName(PAYMENT_GATEWAY_TAB);
  if (!paymentSheet) {
    throw new Error('Sheet not found: ' + PAYMENT_GATEWAY_TAB);
  }
  var lastRow = Math.max(1, paymentSheet.getLastRow());
  PropertiesService.getScriptProperties().setProperty(PG_LAST_ROW_PROP, String(lastRow));
  Logger.log('Seeded PaymentGateway cursor at row ' + lastRow + ' (existing rows ignored)');
}

/**
 * Main sync — only rows after the saved cursor. Safe to run manually or from trigger.
 */
function syncPaymentGatewayToNewsletter() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    Logger.log('Skipped: another sync is already running');
    return { ok: false, error: 'locked' };
  }

  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var paymentSheet = ss.getSheetByName(PAYMENT_GATEWAY_TAB);
    if (!paymentSheet) {
      throw new Error('Sheet not found: ' + PAYMENT_GATEWAY_TAB);
    }
    var newsletterSheet = getOrCreateNewsletterSheetPg_(ss);

    var lastRow = paymentSheet.getLastRow();
    if (lastRow < 2) {
      Logger.log('PaymentGateway has no data rows');
      return { ok: true, added: 0, skipped: 0, invalid: 0 };
    }

    var props = PropertiesService.getScriptProperties();
    var cursor = parseInt(props.getProperty(PG_LAST_ROW_PROP) || '0', 10);
    if (!cursor || cursor < 1) {
      // First run without install seed: start from end (new rows only)
      props.setProperty(PG_LAST_ROW_PROP, String(lastRow));
      Logger.log('No cursor yet — seeded at row ' + lastRow + ' (skipped backfill)');
      return { ok: true, added: 0, skipped: 0, invalid: 0, seeded: lastRow };
    }

    if (lastRow <= cursor) {
      Logger.log('No new PaymentGateway rows (cursor=' + cursor + ', lastRow=' + lastRow + ')');
      return { ok: true, added: 0, skipped: 0, invalid: 0 };
    }

    // Header row + new data rows only
    var headerValues = paymentSheet.getRange(1, 1, 1, paymentSheet.getLastColumn()).getValues()[0];
    var headers = headerValues.map(function (h) {
      return String(h || '')
        .trim()
        .toLowerCase();
    });
    var emailCol = findColumnIndexPg_(headers, EMAIL_HEADER_ALIASES);
    var nameCol = findColumnIndexPg_(headers, NAME_HEADER_ALIASES);
    if (emailCol < 0) {
      throw new Error(
        'No email column in PaymentGateway. Headers: ' + headerValues.join(', ')
      );
    }

    var startRow = cursor + 1;
    var numRows = lastRow - cursor;
    var paymentValues = paymentSheet
      .getRange(startRow, 1, numRows, paymentSheet.getLastColumn())
      .getValues();

    var existing = loadNewsletterEmailIndexPg_(newsletterSheet);
    var added = 0;
    var skipped = 0;
    var invalid = 0;
    var seenInBatch = {};

    for (var i = 0; i < paymentValues.length; i++) {
      var row = paymentValues[i];
      if (emailCol >= row.length) continue;

      var email = String(row[emailCol] || '')
        .trim()
        .toLowerCase();
      if (!email) continue;
      if (!EMAIL_RE.test(email)) {
        invalid++;
        continue;
      }
      if (seenInBatch[email] || existing[email]) {
        skipped++;
        continue;
      }
      seenInBatch[email] = true;

      var name =
        nameCol >= 0 && nameCol < row.length ? String(row[nameCol] || '').trim() : '';

      newsletterSheet.appendRow([
        email,
        name,
        PAYMENT_GATEWAY_SOURCE,
        mytNow(),
        '0',
        '',
        'active',
        ''
      ]);
      existing[email] = true;
      added++;
    }

    props.setProperty(PG_LAST_ROW_PROP, String(lastRow));

    var summary = {
      ok: true,
      added: added,
      skipped: skipped,
      invalid: invalid,
      fromRow: startRow,
      toRow: lastRow
    };
    Logger.log(
      'PaymentGateway → Newsletter (new only): added=' +
        added +
        ' skipped=' +
        skipped +
        ' invalid=' +
        invalid +
        ' rows=' +
        startRow +
        '-' +
        lastRow
    );
    return summary;
  } finally {
    lock.releaseLock();
  }
}

function getOrCreateNewsletterSheetPg_(ss) {
  var sheet = ss.getSheetByName(NEWSLETTER_TAB);
  if (!sheet) {
    sheet = ss.insertSheet(NEWSLETTER_TAB);
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
    return sheet;
  }
  var values = sheet.getDataRange().getValues();
  if (values.length === 0) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  }
  return sheet;
}

function loadNewsletterEmailIndexPg_(sheet) {
  var values = sheet.getDataRange().getValues();
  var index = {};
  for (var r = 1; r < values.length; r++) {
    var email = String(values[r][0] || '')
      .trim()
      .toLowerCase();
    if (!email) continue;
    index[email] = true;
  }
  return index;
}

function findColumnIndexPg_(headers, aliases) {
  for (var i = 0; i < headers.length; i++) {
    if (aliases[headers[i]]) return i;
  }
  return -1;
}
