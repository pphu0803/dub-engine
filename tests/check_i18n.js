// CI gate: the zh and en dictionaries in extension/i18n.js must have identical
// key sets, and every {param} placeholder in zh must exist in en (and vice versa).
const DubI18n = require("../extension/i18n.js");
const { zh, en } = DubI18n.dict;

const missing = Object.keys(zh).filter((k) => !(k in en))
  .concat(Object.keys(en).filter((k) => !(k in zh)));
if (missing.length) {
  console.error("missing keys:", missing);
  process.exit(1);
}

const params = (s) => (String(s).match(/\{[a-z]+\}/g) || []).sort().join();
for (const k of Object.keys(zh)) {
  if (params(zh[k]) !== params(en[k])) {
    console.error(`param mismatch: ${k}  zh=${params(zh[k])}  en=${params(en[k])}`);
    process.exit(1);
  }
}
console.log(`i18n parity ok: ${Object.keys(zh).length} keys`);
