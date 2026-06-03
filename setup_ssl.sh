#!/bin/bash
# setup_ssl.sh — Generate a self-signed SSL certificate for local HTTPS
# Run once: bash setup_ssl.sh
# Then visit: https://localhost:5001 (accept the browser warning)

echo "Generating self-signed SSL certificate..."

openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout cert.key \
  -out cert.crt \
  -days 365 \
  -subj "/C=NZ/ST=Wellington/L=Wellington/O=Silver Fern Consulting Ltd/CN=localhost"

echo ""
echo "✅ Certificate generated: cert.crt and cert.key"
echo ""
echo "To enable HTTPS, update the last line of app.py from:"
echo "  app.run(debug=True, host='0.0.0.0', port=5001)"
echo "to:"
echo "  app.run(debug=True, host='0.0.0.0', port=5001, ssl_context=('cert.crt', 'cert.key'))"
echo ""
echo "Then visit https://localhost:5001"
echo "Your browser will show a security warning — click 'Advanced' → 'Proceed' to accept."
echo ""
echo "NOTE: For production hosting (Railway/Render), SSL is automatic and free."
echo "No self-signed cert needed — HTTPS works out of the box."
