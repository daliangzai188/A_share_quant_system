"""账户展示脱敏；仅用于日志/报告，不能改写券商请求和交易账本身份。"""


def mask_account_id(value):
    value = str(value or '').strip()
    return '***' + value[-2:] if value else '***'


def public_account_data(value):
    """生成展示副本；原始券商对象不变，证券名称/代码不受影响。"""
    if isinstance(value, (list, tuple)):
        return [public_account_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        normalized = str(key).replace('_', '').lower()
        if normalized in {'accountid', 'account', 'fundaccount', 'fundaccountid', 'mstraccountid',
                          'mstrfundaccount', 'mstrstockaccount', 'mstrshareholderaccount', '资金账号', '资金账户'}:
            result[key] = mask_account_id(item)
        elif normalized in {'accountname', 'customername', 'clientname', 'holdername',
                            'mstraccountname', 'mstrcustomername', 'mstrholdername', '账号名称', '账户名称'}:
            result[key] = '***'
        elif normalized == 'raw':
            # 原生扩展字段可能附带姓名/账号的自由文本，不进入面向用户的CSV。
            continue
        else:
            result[key] = public_account_data(item)
    return result
