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
    getElementById: (id) => new DummyElement(),
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
    const script = fs.readFileSync('web_interface/static/collections.js', 'utf8')
                     .replace('let PE_METRICS', 'var PE_METRICS')
                     .replace('let pe_selectedId', 'var pe_selectedId');
    eval(script);

    // Create 100 fake donations
    const sample_data = [];
    for(let i=0; i<100; i++) {
        sample_data.push({
            "collection_id": "test" + i,
            "total_events": (Math.random() * 1000).toString(),
            "chattiness": (Math.random() * 10).toString(),
            "annotation_tags": []
        });
    }

    pe_init();
    pe_handleStatsData(sample_data);
    
    // Explicitly test pe_createStrip for a metric that might have missing data
    console.log("Testing create strip for 'total_events'");
    let el = pe_createStrip("total_events");
    console.log("Returned row children count:", el.children.length); // Should be header, boxes, axis (3)
    
    console.log("Testing create strip for a missing metric 'emoji_rate'");
    let el2 = pe_createStrip("emoji_rate");
    console.log("Returned row 2 children count:", el2.children.length);
    
    console.log("All renders successful");
} catch(e) {
    console.error("ERROR CAUGHT:");
    console.error(e);
}
