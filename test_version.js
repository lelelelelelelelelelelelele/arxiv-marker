const { Services } = require("resource://gre/modules/Services.jsm");
console.log(Services.vc.compare("9.0.4", "10.99.99"));
