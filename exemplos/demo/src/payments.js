// Already in the repo: the body that gets "rediscovered" under another name.
function handleCreditError(error, order) {
    const code = error && error.code ? error.code : 'UNKNOWN'
    logger.warn('payment failed', { code, order: order.id })
    if (code === 'INSUFFICIENT_FUNDS') {
        return { ok: false, reason: 'funds', retry: false }
    }
    if (code === 'TIMEOUT') {
        return { ok: false, reason: 'network', retry: true }
    }
    return { ok: false, reason: 'unknown', retry: true }
}

function validateCard(number) {
    return typeof number === 'string' && number.length >= 13
}
module.exports = { handleCreditError, validateCard }
