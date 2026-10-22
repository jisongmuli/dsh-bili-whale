"""Bilibili's native reply-composer '转到动态' switch, in the comment request itself."""
import json
from urllib.parse import urlsplit
from bilibili_api import comment
from bilibili_api.dynamic import upload_image
from bilibili_api.utils.network import Api


def image_identity(url):
    parsed = urlsplit(str(url).replace('http://', 'https://', 1))
    host = (parsed.hostname or '').lower()
    return ('hdslb.com' if host.endswith('.hdslb.com') else host), parsed.path.split('@', 1)[0]


def image_evidence(reply, expected_url):
    pictures = (reply.get('content') or {}).get('pictures') or []
    attached = any(image_identity(p.get('img_src') or p.get('src')) == image_identity(expected_url) for p in pictures)
    return {'image_attached': attached, 'picture_count': len(pictures)}


async def verify_image_comment(oid, type_, rpid, expected_url, root=None):
    """Only reads public Bili endpoints. Never retries a comment submission."""
    import httpx
    params = {'type': type_.value, 'oid': oid, 'root': root or rpid, 'ps': 20, 'pn': 1}
    checks = [('/x/v2/reply/reply', params),
              ('/x/v2/reply/main', {'type': type_.value, 'oid': oid, 'mode': 2, 'next': 0, 'ps': 20}),
              ('/x/v2/reply', {'type': type_.value, 'oid': oid, 'sort': 0, 'pn': 1, 'ps': 20})]
    async with httpx.AsyncClient(timeout=12, trust_env=False, headers={
        'Referer': 'https://www.bilibili.com/', 'User-Agent': 'Mozilla/5.0'}) as client:
        for path, query in checks:
            try:
                data = (await client.get('https://api.bilibili.com' + path, params=query)).json()
                if data.get('code') != 0:
                    continue
                body = data.get('data') or {}
                rows = [body.get('root') or {}] + (body.get('replies') or [])
                for row in rows:
                    if str(row.get('rpid_str') or row.get('rpid')) == str(rpid):
                        return {**image_evidence(row, expected_url), 'verified_visible': not bool(row.get('invisible'))}
            except Exception:
                continue
    return {'image_attached': False, 'picture_count': None, 'verified_visible': False}


async def send_comment_native(text, oid, type_, credential, root=None, parent=None, pic=None, sync_to_dynamic=True):
    import conversation as c
    from reply_controls import require_allowed
    if c.contains_outbound_private_info(text):
        raise ValueError(c.OUTBOUND_PRIVACY_ERROR)
    require_allowed(oid, type_, text, root, parent)
    credential.raise_for_no_sessdata()
    credential.raise_for_no_bili_jct()
    if root is None and parent is not None:
        raise ValueError('parent requires root')
    # Verified in the official Android client's BiliCommentApiService.postComment:
    # model.b.v places plat="2" and String.valueOf(syncToDynamic) in this form.
    data = {'oid': oid, 'type': type_.value, 'message': text, 'plat': 2,
            'from': '0', 'sync_to_dynamic': 'true' if sync_to_dynamic else 'false'}
    if root is not None:
        data.update(root=root, parent=root if parent is None else parent)
    if pic:
        uploaded = await upload_image(image=pic, credential=credential)
        if not text.strip():
            import conversation as c
            policy = c.read_json(c.STATE_FILE.parent/'comment-image-policy.json', {})
            caption = str(policy.get('empty_text_caption') or '')
            if policy.get('allow_minimal_caption') is True and 1 <= len(caption) <= 12 and not c.contains_internal_control(caption):
                data['message'] = caption
        # Picture-composer form, with the native sync switch in the same
        # comment request. Never substitute an image name for the attachment.
        data.update(plat=1, statistics='{"appId":100,"platform":5}', gaia_source='main_web',
                    at_name_to_mid='{}', has_vote_option='false',
                    sync_to_dynamic=1 if sync_to_dynamic else 0)
        data['pictures'] = json.dumps([{
            'img_src': uploaded['image_url'], 'img_width': uploaded['image_width'],
            'img_height': uploaded['image_height'], 'img_size': uploaded['img_size']}])
    require_allowed(oid, type_, text, root, parent)
    result = await Api(**comment.API['comment']['send'], credential=credential).update_data(**data).result
    if pic:
        evidence = image_evidence(result.get('reply') or {}, uploaded['image_url'])
        rpid = str(result.get('rpid_str') or result.get('rpid') or (result.get('reply') or {}).get('rpid') or '')
        public = await verify_image_comment(oid, type_, rpid, uploaded['image_url'], root) if rpid.isdigit() else {}
        result['_image_receipt'] = {'uploaded': True, 'url': uploaded['image_url'],
            'submitted_text': data['message'],
            'image_attached': evidence['image_attached'] or public.get('image_attached', False),
            'response_picture_count': evidence['picture_count'],
            'public_picture_count': public.get('picture_count'),
            'verified_visible': bool(public.get('verified_visible') and public.get('image_attached'))}
    return result
