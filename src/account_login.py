"""Manufacturer SMS login. Never log requests, responses or account identifiers."""
from __future__ import annotations
import hashlib
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

API_ROOT = 'https://gw.papahuoban.cn'
SMS_PATH = '/user/user/sms'
LOGIN_PATH = '/auth/oauth2/token?grant_type=papazhua-app-sms&scope=server'
# Public app client identifier, not an account credential.
CLIENT_AUTH = 'Basic cGFwYXpodWEtYXBwOnBhcGF6aHVhLWFwcA=='


class LoginError(ValueError):
    """Only fixed, safe messages may be returned to the browser."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LoginError('厂商登录接口发生重定向，已停止；请等待兼容性更新')


def sign(path, timestamp):
    value = f'uri:{urlsplit(path).path} params:{{}} Timestamp:{timestamp}'
    return hashlib.md5(value.encode()).hexdigest().upper()


def credentials(value):
    if not isinstance(value, dict):
        raise LoginError('登录凭据格式不完整')
    result = {key: value.get(key) for key in ('user_id', 'token')}
    if any(not isinstance(item, str) or not item.strip() or len(item) > 4096
           for item in result.values()) or len(json.dumps(result).encode()) > 8192:
        raise LoginError('登录凭据格式不完整')
    return result


def phone_number(value):
    if not isinstance(value, str) or not re.fullmatch(r'1[3-9][0-9]{9}', value):
        raise LoginError('请填写 11 位中国大陆手机号')
    return value


def post(path, body):
    timestamp = str(int(time.time()))
    headers = {'Content-Type': 'application/json;charset=UTF-8',
               'Content-IDType': 'application/json;charset=UTF-8', 'X-Access-Token': '',
               'User-Agent': 'android', 'Sign': sign(path, timestamp),
               'X-APP-Version': '2.2.0', 'Timestamp': timestamp, 'Accept-language': 'zh-CN'}
    if path == LOGIN_PATH:
        headers['Authorization'] = CLIENT_AUTH
    request = Request(API_ROOT + path, json.dumps(body, separators=(',', ':')).encode(), headers)
    try:
        # urllib verifies HTTPS certificates; never retry a message or follow redirects.
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise LoginError('厂商响应无法识别，请等待兼容性更新')
        result = json.loads(payload)
    except HTTPError as error:
        error.close()
        if error.code == 429:
            raise LoginError('厂商限制了请求频率，请稍后手动重试') from None
        raise LoginError('厂商未接受登录请求，请检查验证码或稍后重试') from None
    except (URLError, OSError):
        raise LoginError('无法安全连接厂商登录服务，请检查网络与系统时间') from None
    except (ValueError, UnicodeError):
        raise LoginError('厂商响应无法识别，请等待兼容性更新') from None
    if not isinstance(result, dict) or result.get('success') is False:
        raise LoginError('厂商未接受请求，请检查手机号、验证码或 App 提示')
    if 'code' in result and str(result['code']) not in ('0', '200'):
        raise LoginError('厂商未接受请求；如需验证，请先在官方 App 完成')
    return result


def send_code(phone):
    result = post(SMS_PATH, {'phoneNumber': phone_number(phone), 'type': 0})
    if result.get('success') is not True and str(result.get('code')) not in ('0', '200'):
        raise LoginError('无法确认短信是否发送，请稍后查看手机；不会自动重发')


def login(phone, code):
    phone = phone_number(phone)
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4,8}', code):
        raise LoginError('请填写短信中的数字验证码')
    result = post(LOGIN_PATH, {'mobile': phone, 'captcha': code, 'deviceToken': ''})
    # Actual app responses use data; the archived prototype incorrectly assumed result.
    data = result.get('data', result.get('result'))
    try:
        if isinstance(data, str):
            data = json.loads(data)
        user = data.get('user') if isinstance(data, dict) else None
        user_id = user.get('id') if isinstance(user, dict) else None
        if type(user_id) is int:
            user_id = str(user_id)
        return credentials({'user_id': user_id, 'token': data.get('token')})
    except (ValueError, AttributeError):
        raise LoginError('登录响应缺少必要凭据，请等待兼容性更新') from None
