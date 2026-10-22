export const BROWSE_MIN_GAP_MS = 30 * 60 * 1000

export function inBrowseHours(now = new Date()) {
  const hour = Number(new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', hour: 'numeric', hourCycle: 'h23',
  }).format(now))
  return hour >= 8 && hour < 23
}

export function nextBrowseAt(now = new Date()) {
  return now.getTime() + BROWSE_MIN_GAP_MS
}
