const fs = require('fs');
const updatePath = '/tmp/workspace/lelelelelelelelelelelelele/arxiv-marker/plugin/update.json';
const update = JSON.parse(fs.readFileSync(updatePath, 'utf8'));

if (update.addons && update.addons["arxiv-marker@lele.dev"]) {
    update.addons["arxiv-marker@lele.dev"].updates.forEach(u => {
        if (u.applications && u.applications.zotero) {
            u.applications.zotero.strict_max_version = "99.99.99";
        }
    });
}

fs.writeFileSync(updatePath, JSON.stringify(update, null, 2));
