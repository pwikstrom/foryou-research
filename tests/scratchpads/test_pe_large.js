const fs = require('fs');

class DummyElement {
    constructor(tag) { this.tag=tag; Object.assign(this, {classList: {add: ()=>{}, remove: ()=>{}}, dataset: {}, style: {}}); this.children = []; }
    appendChild(child) { this.children.push(child); }
    addEventListener() {}
    querySelector() { return new DummyElement(); }
    querySelectorAll() { return [new DummyElement(), new DummyElement()]; }
    closest() { return new DummyElement(); }
}

global.document = {
    getElementById: (id) => {
        let el = new DummyElement();
        el.innerHTML = "";
        return el;
    },
    createElement: (tag) => new DummyElement(tag),
    querySelectorAll: () => []
};
global.window = {};
global.Plotly = {react: () => {}};
global.localStorage = {
    getItem: () => null,
    setItem: () => {}
};
global.fetch = () => Promise.resolve({json: () => Promise.resolve({})});

try {
    const content = fs.readFileSync('web_interface/static/collections.js', 'utf8');
    eval(content);

    // Create 150 fake s
    const sample_data = [];
    for(let i=0; i<150; i++) {
        let d = {
            "collection_id": "test" + i,
            "annotation_tags": []
        };
        // Provide messy numeric data for all available metrics
        for(let key of Object.keys(PE_METRIC_INFO)) {
             if(Math.random() < 0.1) d[key] = null;
             else if(Math.random() < 0.1) d[key] = undefined;
             else if(Math.random() < 0.1) d[key] = "NaN";
             else if(Math.random() < 0.1) d[key] = "some_string";
             else d[key] = (Math.random() * 100).toString();
        }
        sample_data.push(d);
    }

    pe_init();
    
    // Explicitly test the percentile ranks calculation which might fail on bad data
    pe_data = sample_data;
    pe_calculatePercentileRanks();
    console.log("Calculated percentile ranks successfully");
    
    // Test render all
    pe_renderAllStrips();
    console.log("Rendered all strips successfully");
    

} catch(e) {
    console.error("ERROR CAUGHT:");
    console.error(e);
}
