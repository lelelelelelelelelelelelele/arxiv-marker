const fs = require('fs');
const manifestPath = '/tmp/workspace/lelelelelelelelelelelelele/arxiv-marker/plugin/manifest.json';
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

manifest.browser_specific_settings = JSON.parse(JSON.stringify(manifest.applications));

// Fix strict_max_version just in case
manifest.applications.zotero.strict_max_version = "99.99.99";
manifest.browser_specific_settings.zotero.strict_max_version = "99.99.99";

fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
