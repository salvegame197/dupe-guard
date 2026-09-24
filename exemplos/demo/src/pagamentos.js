// Existente no repo: o corpo que sera "redescoberto" com outro nome.
function handleCreditError(erro, pedido) {
    const codigo = erro && erro.code ? erro.code : 'DESCONHECIDO'
    logger.warn('falha no pagamento', { codigo, pedido: pedido.id })
    if (codigo === 'SALDO_INSUFICIENTE') {
        return { ok: false, motivo: 'saldo', tentarNovamente: false }
    }
    if (codigo === 'TIMEOUT') {
        return { ok: false, motivo: 'rede', tentarNovamente: true }
    }
    return { ok: false, motivo: 'desconhecido', tentarNovamente: true }
}

function validarCartao(numero) {
    return typeof numero === 'string' && numero.length >= 13
}
module.exports = { handleCreditError, validarCartao }
