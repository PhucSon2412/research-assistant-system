Optional CA bundle for a private SMTP relay, mounted read-only at
`/etc/ras/trust` in the API container (`RAS_SMTP_CA_FILE=/etc/ras/trust/<file>`).
Leave `RAS_SMTP_CA_FILE` empty to use the image's system CAs. Keep real files
outside Git and point `RAS_TRUST_DIR` at them.
