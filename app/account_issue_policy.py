"""Separate account execution events from business decisions and hard failures."""
FORMAT_CODES = {'OUTPUT_CONTRACT_ERROR', 'INVALID_JSON', 'INVALID_OUTPUT',
                'QODER_RECOGNITION_FORMAT', 'QODER_RECOGNITION_SCHEMA',
                'ZCODE_RECOGNITION_FORMAT', 'ZCODE_RECOGNITION_SCHEMA'}
UNKNOWN_CODES = {'RESPONSE_UNKNOWN', 'ZCODE_RESPONSE_UNKNOWN',
                 'ZCODE_REQUEST_INCOMPLETE', 'ZCODE_NO_AUTOMATIC_RESEND'}
IDENTITY_CODES = {'ACCOUNT_RESULT_IDENTITY', 'ACCOUNT_SESSION_CHANGED',
                  'ACCOUNT_TASK_IDENTITY', 'REQUEST_IDENTITY_MISMATCH',
                  'PPT_SLIDE_ID_MISMATCH', 'QODER_RECOGNITION_ID_MISMATCH',
                  'ZCODE_RECOGNITION_ID_MISMATCH'}
AUTH_CODES = {'QODER_NOT_READY', 'ZCODE_NOT_READY', 'ZCODE_DISABLED', 'ZCODE_MODEL_UNAVAILABLE',
              'QODER_REQUEST_ERROR', 'PROVIDER_UNAVAILABLE',
              'PROVIDER_AUTH_REQUIRED', 'AUTH_REQUIRED', 'AUTH_STATE_UNKNOWN',
              'ACCOUNT_MODEL_CHANGED', 'QODER_MODEL_LOCKED', 'ZCODE_MODEL_LOCKED'}
BUDGET_CODES = {'ACCOUNT_TURN_BUDGET', 'QODER_BUDGET_EXHAUSTED', 'ZCODE_RUN_BUDGET',
                'ZCODE_PLAN_INVALID', 'ZCODE_PLAN_IDENTITY', 'PPT_TIME_BUDGET'}
TOOL_CODES = {'ACCOUNT_PERMISSION_UNSUPPORTED', 'ZCODE_UNEXPECTED_TOOL_RESULT'}


def classify(code):
    if code in UNKNOWN_CODES or 'UNKNOWN' in code:
        return 'interrupted', '请求结果尚未确认，已保留原会话；先核对原结果，不自动重复发送。'
    if code in FORMAT_CODES:
        return 'needs_attention', '已收到回复，但自动格式整理未通过；原内容已保留，无需人工修改JSON。'
    if code in IDENTITY_CODES:
        return 'needs_attention', '回复与原任务身份不一致，已隔离保留；程序不能将其他块或会话的内容并入本任务。'
    if code in AUTH_CODES:
        return 'needs_attention', '连接或模型设置需要处理，请到AI引擎设置检查；已有任务和内容保留。'
    if code in BUDGET_CODES:
        return 'needs_attention', '本轮执行预算或计划条件需要处理；已有内容保留，未自动扩大预算。'
    if code in TOOL_CODES:
        return 'needs_attention', 'Agent请求的工具操作超出当前内容通道；操作未执行，原会话和内容已保留。'
    if code == 'QODER_OUTPUT_TRUNCATED':
        return 'needs_attention', '回复达到输出上限，完整内容尚未返回；原答保留，由程序按既定预算评估分块或接续，不能把截断内容当作完成。'
    if code in {'ZCODE_NOT_DISPATCHED', 'ZCODE_EMPTY_RESPONSE', 'ACCOUNT_SESSION_MISSING', 'ACCOUNT_QUESTION_INVALID'}:
        return 'needs_attention', '当前执行条件或回复需要处理，已有记录保留；未自动重复发送。'
    return 'failed', '执行发生无法自动处理的错误，原记录和已有内容保留。'
