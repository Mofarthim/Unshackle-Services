const crypto = require('crypto').webcrypto;

function ne(e) {
    return btoa(String.fromCharCode.apply(null, Array.from(e))).split("=").join("").split("+").join("-").split("/").join("_")
}
async function re(e, t) {
    const n = await crypto.subtle.sign("HMAC", e, t);
    return new Uint8Array(n)
}
async function ie(e, t, n) {
    let r = t;
    for (let t = 0; t < n + 1; t += 1)
        r = await re(e, r);
    return r
}
async function oe(e, t, n) {
    const r = await async function(e) {
        const t = (new TextEncoder).encode(e);
        return crypto.subtle.importKey("raw", t, {
            name: "HMAC",
            hash: {
                name: "SHA-256"
            }
        }, !1, ["sign"])
    }(e), i = new TextEncoder, o = await ie(r, i.encode(e), n.getUTCMonth() + 1).then((e => ie(r, i.encode(ne(e) + t), n.getUTCDate() % 5))).then((e => ie(r, i.encode(ne(e) + String((0,
        z)(n.getTime()))), n.getUTCHours() % 5)));
    return ne(o)
}

function ae(e, t) {
    return oe("v+Gjs=25Aw5erR!J8ZuvRrCx*rGswhB&qdHd_SYerEWdU&a?3DzN9BRbp5KwY4hEmcj5#fykMjJ=AuWz5GSMY-d@H7DMEh3M@9n2G552Us$$k9cD=3TxwWe86!x#Zyhe", e, t)
}

function K(e) {
    return e ?? Date.now()
}

function z(e) {
    const t = K(e);
    return Math.floor(t / 1e3)
}

function genId() {
    for (var e, n = "", r = 0; r < 32; r++)
        e = 16 * Math.random() | 0,
        r > 4 && r < 21 && !(r % 4) && (n += "-"),
        n += (12 === r ? 4 : 16 === r ? 3 & e | 8 : e).toString(16);
    return n
}

function te() {
    const e = new Date;
    return e.setHours(e.getHours() + 1),
        e.setMinutes(0),
        e.setSeconds(0),
        e
}

async function genJson() {
    var e = genId()
    return {
        deviceId: e,
        applicationKeySecret: await ae(e, te())
    }
}

genJson().then((result) => console.log(JSON.stringify(result)));
