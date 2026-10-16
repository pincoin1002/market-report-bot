import { timingSafeEqual, createHash } from 'node:crypto';

const REPO = 'pincoin1002/market-report-bot';
const SPECS = {
  tw_open: { workflow: 'tw-open.yml', zone: 'Asia/Taipei', start: '07:40', end: '08:45', type: 'tw_open' },
  tw_close: { workflow: 'tw-close.yml', zone: 'Asia/Taipei', start: '15:05', end: '18:00', type: 'tw_close' },
  edt: { workflow: 'us-open.yml', zone: 'America/New_York', start: '09:05', end: '09:25', type: 'us_open', offset: '-04:00' },
  est: { workflow: 'us-open.yml', zone: 'America/New_York', start: '09:05', end: '09:25', type: 'us_open', offset: '-05:00' },
  us_close: { workflow: 'us-close.yml', zone: 'Asia/Taipei', start: '08:30', end: '12:00', type: 'us_close' },
};

export function classify(slot, now = new Date()) {
  const spec = SPECS[slot];
  if (!spec) return { status: 'NOT_FOUND' };
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone: spec.zone,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    weekday: 'short', hourCycle: 'h23', timeZoneName: 'longOffset' }).formatToParts(now).map(p => [p.type, p.value]));
  const day = `${parts.year}-${parts.month}-${parts.day}`;
  const time = `${parts.hour}:${parts.minute}`;
  if (spec.offset && !parts.timeZoneName.endsWith(spec.offset)) return { status: 'INACTIVE_DST_SLOT' };
  const weekend = spec.type === 'us_close' ? ['Sun', 'Mon'] : ['Sat', 'Sun'];
  if (weekend.includes(parts.weekday)) return { status: 'MARKET_CLOSED' };
  if (time < spec.start || time >= spec.end) return { status: 'OUTSIDE_DELIVERY_WINDOW' };
  // US-close delivery identity is the TPE publication date, as confirmed by
  // production receipts; it is NOT the prior NYSE quote session date.
  return { status: 'DISPATCH', spec, day, key: `${spec.type}:${day.replaceAll('-', '')}` };
}

export function receiptValid(receipt, key) {
  return receipt?.key === key && receipt.state === 'DELIVERED'
    && Array.isArray(receipt.telegram_message_ids) && receipt.telegram_message_ids.length > 0
    && receipt.telegram_message_ids.every(id => Number.isSafeInteger(id) && id > 0);
}

export async function handle(slot, authorization, { now = new Date(), env = process.env, fetcher = fetch } = {}) {
  const secret = env.CRON_SECRET;
  const digest = x => createHash('sha256').update(x).digest();
  if (!secret || !timingSafeEqual(digest(authorization || ''), digest(`Bearer ${secret}`))) return [401, { status: 'UNAUTHORIZED' }];
  const decision = classify(slot, now);
  if (decision.status !== 'DISPATCH') return [decision.status === 'NOT_FOUND' ? 404 : 200, { status: decision.status }];
  if (!env.GITHUB_WORKFLOW_DISPATCH_TOKEN) return [503, { status: 'DISPATCH_CREDENTIAL_UNAVAILABLE' }];
  const headers = { Authorization: `Bearer ${env.GITHUB_WORKFLOW_DISPATCH_TOKEN}`, Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' };
  try {
    const path = `reports/.delivery/${decision.key.replace(':', '_')}.json`;
    const response = await fetcher(`https://api.github.com/repos/${REPO}/contents/${path}?ref=main`,
      { headers, signal: AbortSignal.timeout(8000) });
    if (response.status === 200) {
      const metadata = await response.json();
      const receipt = JSON.parse(Buffer.from(metadata.content, 'base64').toString('utf8'));
      return receiptValid(receipt, decision.key) ? [200, { status: 'ALREADY_DELIVERED', key: decision.key }]
        : [409, { status: 'DELIVERY_RECEIPT_UNCERTAIN' }];
    }
    if (response.status !== 404) return [503, { status: 'RECEIPT_CHECK_UNAVAILABLE' }];
    const inputs = { send_telegram: 'true' };
    if (decision.spec.type === 'us_close') inputs.publication_date = decision.day;
    if (decision.spec.type === 'tw_open') inputs.trigger_source = 'vercel_cron';
    if (decision.spec.type === 'us_open') Object.assign(inputs, { intended_market_date: decision.day,
      intended_market_time: '09:05', trigger_source: 'vercel_cron', scheduler_triggered_at: now.toISOString() });
    const sent = await fetcher(`https://api.github.com/repos/${REPO}/actions/workflows/${decision.spec.workflow}/dispatches`,
      { method: 'POST', headers, body: JSON.stringify({ ref: 'main', inputs }), signal: AbortSignal.timeout(8000) });
    return sent.status === 204 ? [202, { status: 'DISPATCHED', key: decision.key }]
      : [502, { status: 'DISPATCH_REJECTED', http_status: sent.status }];
  } catch {
    // An ambiguous dispatch timeout is not retried: workflow delivery concurrency
    // and fresh receipt checks handle later independent scheduler invocations.
    return [503, { status: 'DISPATCH_OR_RECEIPT_UNCONFIRMED' }];
  }
}

export default async function handler(req, res) {
  const url = new URL(req.url, 'https://scheduler.invalid');
  const [status, result] = await handle(url.searchParams.get('slot'), req.headers.authorization);
  console.log(JSON.stringify({ scheduler_invoked_at: new Date().toISOString(), status: result.status, key: result.key }));
  res.status(status).json(result);
}
