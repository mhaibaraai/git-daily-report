"""Text-only proposals with complete original report snapshots."""

from datetime import datetime

from .common import ReportError, day_string, identifier, keys, number
from .archive import archive_report
from .eom import EomClient, normalize
from .store import load_report, lock, report_path, write_state
from .validation import generated_progress, summary, validate_days


SNAPSHOT_FIELDS = {'id', 'memo', 'ewdProblem', 'tomorrowPlan', 'ewdRate', 'ewdWorkRealTimes'}


def validate_snapshot(value):
    keys(value, SNAPSHOT_FIELDS, SNAPSHOT_FIELDS)
    identifier(value['id'])
    if any(not isinstance(value[key], str) for key in ('memo', 'ewdProblem', 'tomorrowPlan')):
        raise ReportError('Original EOM text is missing or invalid.')
    rate, hours = number(value['ewdRate']), number(value['ewdWorkRealTimes'])
    if rate is None or not 0 <= rate <= 100 or hours is None or not 0 < hours <= 24:
        raise ReportError('Original EOM progress or hours are invalid.')
    return value


def snapshot(progress):
    return validate_snapshot({'id': progress['reportId'], 'memo': progress['memo'],
                              'ewdProblem': progress['issue'], 'tomorrowPlan': progress['plan'],
                              'ewdRate': progress['rate'], 'ewdWorkRealTimes': progress['hours']})


def matches(saved, original):
    if not isinstance(saved, dict):
        return False
    return (all(saved.get(key) == original[key] for key in SNAPSHOT_FIELDS - {'ewdRate', 'ewdWorkRealTimes'})
            and all(number(saved.get(key)) == number(original[key]) for key in ('ewdRate', 'ewdWorkRealTimes')))


def revision_items(payload):
    keys(payload, {'items'}, {'items'})
    if not isinstance(payload['items'], list):
        raise ReportError('items must be an array.')
    seen, result = set(), []
    fields = {'day', 'orderId', 'task', 'data', 'problem', 'issue', 'plan'}
    for entry in payload['items']:
        keys(entry, fields, fields)
        key = (day_string(entry['day']), identifier(entry['orderId']))
        if entry['day'] != key[0] or key in seen:
            raise ReportError('items contains duplicate or noncanonical day/order keys.')
        seen.add(key)
        result.append((key, entry))
    return result


def revise(profile, span, payload, *, client=None):
    # Import locally to keep refresh and proposal creation independent.
    from .workflow import merge_days

    entries = revision_items(payload)
    path = report_path(profile, span)
    with lock(path):
        report = load_report(profile, span)
        client = client or EomClient(profile.eom)
        fresh = client.schedule(span.since, span.until, today=datetime.now(span.timezone).date())
        report['days'] = merge_days(report['days'], fresh)
        index = {(day['day'], order['id']): order for day in report['days'] for order in day['orders']}
        live = {(day, str(raw['id'])): raw for day, orders in fresh.items() for raw in orders}
        changes = []
        for key, entry in entries:
            order, raw = index.get(key), live.get(key)
            progress = normalize(raw)['progress'] if raw else None
            if not order or order.get('stale') or not progress:
                raise ReportError('Text revisions require an existing EOM report in the current schedule.')
            original = snapshot(progress)
            target = generated_progress({**entry, 'rate': progress['rate'], 'hours': progress['hours'],
                                         'estimatedFields': []})
            target.update(source='revise', reportId=progress['reportId'], originalReport=original)
            order['progress'] = progress if matches(snapshot(target), original) else target
            if order['progress'] is target:
                changes.append({'day': key[0], 'orderId': key[1], 'from': original, 'to': snapshot(target)})
        report['days'] = validate_days(report['days'], profile.daily_hours)
        write_state(path, report)
        return {'path': str(path), 'reviewPath': archive_report(profile, report),
                'changes': changes, **summary(report['days'])}
