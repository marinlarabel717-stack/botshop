import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import hashlib
from math import log
import urllib.parse
from collections import OrderedDict
import requests
from urllib.parse import urlencode, quote

api_url = 'https://api.okaypay.me/shop/'
shop_id = ""
shop_token = ""
NAME = ""
bot_username = ""

#OKpay 存款API
def okpay_deposit_api(order_number,amount,coin='USDT',bot_id=None):
    data = {
        'unique_id': order_number,
        'name': f'{NAME}存款',
        'amount': amount,
        'return_url': f'https://t.me/{bot_username}',
        'coin': coin
    }
    data = sign(data)

    deposit_api_url = api_url + 'payLink'
    response = requests.post(deposit_api_url, data=data)
    return response.json()
    
#OKpay 提现API
def okpay_withdraw_api(data ,name = None):
    
    if name is not None:
        data['name'] = name
    data = sign(data)

    withdraw_api_url = api_url + 'transfer'

    # print (data)
    response = requests.post(withdraw_api_url, data=data)
    # print('okpay:',response.json())
    return response.json()

# /**
    # * 检查订单状态
    # * data : {
    # *      order_id 订单号
    # *      unique_id 用户传入的唯一id
    # *      status 订单状态(0:等待中;1:已支付)
    # * }
#*/
def okpay_withdraw_api_checkTransfer(unique_id):
    data = {
        'id': shop_id,
        'token': shop_token,
        'unique_id': unique_id,

    }
    data = sign(data)
    withdraw_api_url = api_url + 'checkTransfer'

    response = requests.post(withdraw_api_url, data=data)
    # print('okpay:',response.json())
    return response.json()


# 数据签名
def sign(data):
    data['id'] = shop_id
    data = {k: v for k, v in data.items() if v}  # 去除空值
    data = OrderedDict(sorted(data.items()))  # 按照key排序
    query = urllib.parse.urlencode(data,quote_via=urllib.parse.quote) # 请求参数拼接
    query = urllib.parse.unquote(query)  # 请求参数解码
    data['sign'] = hashlib.md5((query + '&token=' + shop_token).encode()).hexdigest().upper()
    return data


#将 Python 字典转换为 PHP 风格的查询字符串
def http_build_query(data, prefix=''):
    """递归地将嵌套字典转换为URL查询字符串，包含[]但不编码[]；并保持 + / - 不被编码（用于 type 等值）"""
    result = []
    for key, value in data.items():
        if isinstance(value, dict):
            result.extend(http_build_query(value, f"{prefix}{key}[" if not prefix else f"{prefix}{key}["))
        else:
            # 键：保留 [] 不编码
            encoded_key = quote(f"{prefix}{key}]" if '[' in prefix else f"{prefix}{key}", safe='[]')

            # 值：保留 + 和 - 不编码（否则 + 会变 %2B，签名对不上）
            # 注意：如果你只想对 data[type] 生效，可以在这里加条件判断 key == 'type'
            encoded_value = quote(str(value), safe='+-')

            result.append((encoded_key, encoded_value))
    return result

# 验证签名
def verify_sign(data):
    sign = data.pop('sign')

    # 过滤掉空值
    data = {k: v for k, v in data.items() if v}

    # 对字典进行排序（PHP 常见签名规则）
    sorted_data = dict(sorted(data.items()))

    # 生成查询字符串：不要用 urlencode（它会把空格处理成 +，也容易让 + 语义混乱）
    pairs = http_build_query(sorted_data)
    query_string = "&".join([f"{k}={v}" for k, v in pairs])

    print(query_string)
    return hashlib.md5((query_string + '&token=' + shop_token).encode()).hexdigest().upper() == sign
