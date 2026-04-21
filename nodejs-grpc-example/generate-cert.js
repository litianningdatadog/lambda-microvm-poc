const forge = require('node-forge');
const fs = require('fs');

function generateSelfSignedCert() {
  const keys = forge.pki.rsa.generateKeyPair(2048);
  const cert = forge.pki.createCertificate();
  
  cert.publicKey = keys.publicKey;
  cert.serialNumber = '01';
  cert.validity.notBefore = new Date();
  cert.validity.notAfter = new Date();
  cert.validity.notAfter.setFullYear(cert.validity.notBefore.getFullYear() + 1);
  
  const attrs = [{
    name: 'commonName',
    value: 'localhost'
  }, {
    name: 'organizationName',
    value: 'Example gRPC Node Server'
  }, {
    name: 'organizationalUnitName',
    value: 'Development'
  }, {
    name: 'countryName',
    value: 'US'
  }];
  
  cert.setSubject(attrs);
  cert.setIssuer(attrs);
  
  cert.setExtensions([{
    name: 'basicConstraints',
    cA: true,
    pathLenConstraint: 0,
    critical: true
  }, {
    name: 'subjectAltName',
    altNames: [
      { type: 2, value: 'localhost' },
      { type: 2, value: '127.0.0.1' },
      { type: 2, value: '::1' }
    ]
  }]);
  
  cert.sign(keys.privateKey, forge.md.sha256.create());
  
  const certPem = forge.pki.certificateToPem(cert);
  const keyPem = forge.pki.privateKeyToPem(keys.privateKey);
  
  fs.writeFileSync('server-cert.pem', certPem);
  fs.writeFileSync('server-key.pem', keyPem);
  
  return { cert: certPem, key: keyPem };
}

module.exports = { generateSelfSignedCert };