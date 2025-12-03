import webview
import json
import os
import threading
import time
import sys

if getattr(sys, 'frozen', False):
    # We are running in a bundle (e.g., PyInstaller)
    base_path = os.path.dirname(sys.executable)
else:
    # We are running in a normal Python environment
    base_path = os.path.dirname(os.path.abspath(__file__))

TEMPLATE_DIR = os.path.join(base_path, "templates")


class Api:
    def __init__(self, window):
        self.template = {}
        self.window = window
        self.history = []
        self.history_index = -1
        self._navigating = False

        self.template_dir = TEMPLATE_DIR
        os.makedirs(self.template_dir, exist_ok=True)

    def track_url(self, url):
        if self._navigating:
            self._navigating = False
            return  # skip duplicate during back/forward

        if self.history_index < len(self.history) - 1:
            self.history = self.history[:self.history_index + 1]

        self.history.append(url)
        self.history_index = len(self.history) - 1

    def navigate_to(self, url):
        """
        Processes the URL from the address bar and returns it for JS to load.
        """

        if not url.startswith("http"):
            url = "https://" + url
        return {"status": "navigating", "url": url}

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            url = self.history[self.history_index]
            self._navigating = True
            # Return the URL instead of navigating
            return {'url': url}
        return {'url': None}

    def go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            url = self.history[self.history_index]
            self._navigating = True
            # Return the URL instead of navigating
            return {'url': url}
        return {'url': None}

    def save_selector(self, category, selector):
        self.template[category] = selector
        return {"category": category, "selector": selector}

    def remove_selector(self, category):
        if category in self.template:
            del self.template[category]
        return {"removed": category}

    def get_all_selectors(self):
        return {"selectors": self.template}

    def save_template(self, filename="template.json"):
        # Open save file dialog, starting in our safe template directory
        result = self.window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=self.template_dir,
            save_filename=filename,
            file_types=['JSON Files (*.json)', 'All files (*.*)']
        )

        # --- THIS IS THE MISSING LOGIC ---

        if not result:
            return {"status": "error", "message": "File save cancelled"}

        # pywebview returns a single path string for SAVE dialog
        filepath = result[0] if isinstance(result, (list, tuple)) else result

        if not isinstance(filepath, str):
            return {"status": "error", "message": f"Invalid file path: {filepath}"}

        # Ensure it has a .json extension if user forgot
        if not filepath.lower().endswith('.json'):
            filepath += '.json'

        try:
            # Actually write the template data to the chosen file
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({"selectors": self.template}, f, indent=2)
            return {"status": "ok", "filename": filepath}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def load_template(self, filename=None):
        # Open file dialog, starting in our safe template directory
        result = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=self.template_dir,  # <-- Set the default directory
            file_types=['JSON Files (*.json)', 'All files (*.*)']
        )

        # pywebview returns a list (or tuple) of selected file paths
        if not result:
            return {"status": "error", "message": "File selection cancelled"}

        # Get first selected file
        filepath = result[0] if isinstance(result, (list, tuple)) else result

        if not isinstance(filepath, str) or not os.path.exists(filepath):
            return {"status": "error", "message": f"Invalid file path: {filepath}"}

        # Load JSON
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.template = data.get("selectors", {})
            return {"status": "ok", "selectors": self.template, "filename": filepath}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ------------------- JS Code -------------------
js_code = """
if (!window.__scraper_functions_initialized) {
    window.__scraper_functions_initialized = true;
function safeCall(apiFunc, ...args) {
    return new Promise((resolve, reject) => {
        try {
            pywebview.api[apiFunc](...args).then(resolve).catch(reject);
        } catch(e) {
            console.error(`safeCall ${apiFunc} failed:`, e);
            reject(e);
        }
    });
}

let categorySelections = {}; 
let lastSelectedElement = null;
let isDocked = true;
let pinpointMode = false;
function getXPathPinpointSelector(el) {
    if (!el || !el.tagName) return null;
    let parts = [];
    while (el && el.nodeType === Node.ELEMENT_NODE) {
        let index = 1;
        let sibling = el.previousSibling;
        while (sibling) {
            if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === el.tagName) {
                index++;
            }
            sibling = sibling.previousSibling;
        }
        let part = `*[name()='${el.tagName.toLowerCase()}'][${index}]`;
        parts.unshift(part);
        el = el.parentNode;
    }
    return '/' + parts.join('/');
}
// --- REPLACE your entire getXPathElasticSelector function with this ---
// --- REPLACE your entire getXPathElasticSelector function with this ---

function getXPathElasticSelector(el) {
    if (!el || !el.tagName) return null;
    const originalEl = el; // Keep a reference to the originally clicked element
    const originalTag = originalEl.tagName.toLowerCase();

    // --- Step 0: Prioritize a descriptive class on the element itself ---
    if (el.className && typeof el.className === 'string') {
        const goodClasses = el.className.trim().split(/\s+/).filter(c =>
            c.length > 2 &&
            !/is-|has-|active|open/.test(c) &&
            !/js-/.test(c) &&
            !/\d/.test(c)
        );

        if (goodClasses.length > 0) {
            const tag = el.tagName.toLowerCase();
            return `//${tag}[contains(@class, '${goodClasses[0]}')]`;
        }
    }

    // --- Step 1: The "Smart Label Search" (NEW LOGIC) ---
    // This logic now finds the label and the <dd> "container" separately
    let labelNode = null;
    let targetSibling = null; // This will be the <dd> element
    let searchNode = el;

    for (let i = 0; i < 3; i++) { // Search up to 3 levels up the DOM
        if (!searchNode || searchNode.tagName === 'BODY') break;

        if (searchNode.previousElementSibling) {
            const sibling = searchNode.previousElementSibling;
            const text = (sibling.textContent || "").trim();
            // A "label" often ends with a colon or is a <dt>, <th>, or <label> tag.
            if (text.endsWith(':') || ['DT', 'TH', 'LABEL'].includes(sibling.tagName)) {
                labelNode = sibling;
                targetSibling = searchNode; // This is the <dd> or container
                break;
            }
        }
        searchNode = searchNode.parentElement;
    }

    // --- Step 2: If a label is found, build the best possible XPath (NEW LOGIC) ---
    if (labelNode && targetSibling) { 
        const labelTag = labelNode.tagName.toLowerCase();
        const labelText = (labelNode.textContent || "").trim();

        // This is the tag of the SIBLING (e.g., "dd")
        const targetSiblingTag = targetSibling.tagName.toLowerCase(); 

        // Count how many siblings of the same type are between the label and the target
        let siblingCount = 1;
        let currentSibling = labelNode.nextElementSibling;
        while (currentSibling && currentSibling !== targetSibling) {
            if (currentSibling.tagName === targetSibling.tagName) {
                siblingCount++;
            }
            currentSibling = currentSibling.nextElementSibling;
        }

        // Now, we build the base path to the <dd>
        let baseSelector = `//${labelTag}[normalize-space()='${labelText}']/following-sibling::${targetSiblingTag}[${siblingCount}]`;

        // If the clicked element was *inside* the <dd>, add its tag.
        if (el !== targetSibling) {
            // el is a child of targetSibling. Add its tag.
            // This creates //dt[...]/following-sibling::dd[1]//a
            baseSelector += `//${originalTag}`;
        }

        return baseSelector;
    }


    // --- Step 0.5: Find a stable parent with a good class (Now a fallback) ---
    // This will now correctly find 'intext-links'
    let parent = el.parentElement;
    for (let i = 0; i < 5; i++) { // Search up to 5 levels
        if (!parent || parent.tagName === 'BODY') break;

        if (parent.className && typeof parent.className === 'string') {
            const goodParentClasses = parent.className.trim().split(/\s+/).filter(c =>
                c.length > 2 &&
                !/is-|has-|active|open/.test(c) &&
                !/js-/.test(c) &&
                !/\d/.test(c)
            );

            if (goodParentClasses.length > 0) {
                // We found a good parent!
                const parentTag = parent.tagName.toLowerCase();
                const parentSelector = `//${parentTag}[contains(@class, '${goodParentClasses[0]}')]`;

                // e.g., //div[contains(@class,'intext-links')]//a
                return `${parentSelector}//${originalTag}`;
            }
        }
        parent = parent.parentElement;
    }


    // --- Step 3: If NO label OR parent class, use the stable ancestor fallback (Last resort) ---
    const STABLE_ATTRIBUTES = ['data-testid', 'data-cy', 'data-e2e', 'id', 'name', 'role'];
    let anchor = null;
    let current = el;
    while (current && current.tagName.toLowerCase() !== 'html') {
        for (const attr of STABLE_ATTRIBUTES) {
            if (current.getAttribute(attr)) {
                anchor = current;
                break;
            }
        }
        if (anchor) break;
        current = current.parentElement;
    }
    anchor = anchor || document.body;

    let anchorPath = '';
    if (anchor.tagName.toLowerCase() === 'body') {
        anchorPath = '//body';
    } else {
        for (const attr of STABLE_ATTRIBUTES) {
            const val = anchor.getAttribute(attr);
            if (val) {
                anchorPath = `//${anchor.tagName.toLowerCase()}[@${attr}='${val}']`;
                break;
            }
        }
    }

    if (anchor === el) return anchorPath;

    let relativePath = '';
    let child = el;
    while (child && child !== anchor) {
        let part = child.tagName.toLowerCase();
        let index = 1;
        let sibling = child.previousElementSibling;
        while (sibling) {
            if (sibling.tagName === child.tagName) {
                index++;
            }
            sibling = sibling.previousElementSibling;
        }
        relativePath = `/${part}[${index}]` + relativePath;
        child = child.parentElement;
    }

    return anchorPath + relativePath;
}


// --- NEW: Pinpoint selector (exact element path using :nth-of-type) ---
function getPinpointSelector(el) {
    if (!el) return null;

    let parts = [];
    while (el && el.tagName && el.tagName.toLowerCase() !== 'html') {
        let tag = el.tagName.toLowerCase();

        // prefer IDs if present (they are likely unique) — keeps path short
        if (el.id) {
            parts.unshift('#' + CSS.escape ? ('#' + CSS.escape(el.id)) : ('#' + el.id));
            break;
        }

        const parent = el.parentElement;
        if (!parent) {
            parts.unshift(tag);
            break;
        }

        // compute nth-of-type among element children with same tag
        let idx = 0;
        let count = 0;
        for (let i = 0; i < parent.children.length; i++) {
            const child = parent.children[i];
            if (child.tagName && child.tagName.toLowerCase() === tag) {
                count++;
                if (child === el) {
                    idx = count;
                    break;
                }
            }
        }

        if (idx > 0) {
            parts.unshift(`${tag}:nth-of-type(${idx})`);
        } else {
            // fallback to tag only (shouldn't usually hit)
            parts.unshift(tag);
        }
        el = parent;
    }

    return parts.join(' > ');
}


// --- Generate elastic selector ---
function getElasticSelector(el) {
    if (!el) return null;

    let parts = [];
    while (el && el.tagName.toLowerCase() !== "html") {
        let tag = el.tagName.toLowerCase();

        // If element has stable ID
        if (el.id) {
            if (/\d+$/.test(el.id)) {
                // Strip trailing numbers → [id^="prefix"]
                parts.unshift(`[id^="${el.id.replace(/\d+$/, '')}"]`);
            } else {
                parts.unshift("#" + el.id);
            }
            break; // Stop here, ID is unique enough
        }

        // If element has semantic-looking class
        if (el.className) {
            let classes = el.className.trim().split(/\s+/).filter(c =>
                !c.match(/^\d+$/) &&           // no pure numbers
                !c.match(/^(g-|ods-g-)/) &&    // skip grid/layout
                !c.match(/span|col|row|container|content/) // skip generic layout
            );
            if (classes.length) {
                parts.unshift(tag + "." + classes[0]);
                el = el.parentElement;
                continue;
            }
        }

        // Fallback → just tag
        parts.unshift(tag);
        el = el.parentElement;
    }

    return parts.join(" > ");
}


// --- Merge selectors intelligently ---
function generalizeSegment(a, b) {
    if (a === b) return a;
    let idA = a.match(/\\[id\\^="([a-zA-Z_-]+)"\\]/);
    let idB = b.match(/\\[id\\^="([a-zA-Z_-]+)"\\]/);
    if (idA && idB && idA[1] === idB[1]) return `[id^="${idA[1]}"]`;
    let nthRegex = /(.*):nth-of-type\\(\\d+\\)$/;
    if (nthRegex.test(a) && nthRegex.test(b)) {
        let baseA = a.match(nthRegex)[1];
        let baseB = b.match(nthRegex)[1];
        if (baseA === baseB) return baseA;
    }
    return null;
}
// --- REPLACE your entire getCommonSelector function with this ---

function getCommonSelector(selectors) {
    if (!selectors || selectors.length === 0) return null;
    if (selectors.length === 1) return selectors[0];

    const isXPath = selectors[0].startsWith('/') || selectors[0].startsWith('(');

    if (isXPath) {
        // --- NEW, ROBUST XPATH MERGING LOGIC ---

        // 1. Handle dynamic IDs (like 'ReviewContentsummary2224')
        const idRegex = /^(.*\[)@id='([^']*)'(\].*)$/;
        const firstMatch = selectors[0].match(idRegex);

        if (firstMatch) {
            let commonPrefixLen = -1;
            const idValue = firstMatch[2]; // The ID string itself
            const allMatchPattern = selectors.every(s => s.startsWith(firstMatch[1] + "@id='"));

            if (allMatchPattern) {
                for (let i = 0; i < idValue.length; i++) {
                    if (selectors.every(s => s[firstMatch[1].length + "@id='".length + i] === idValue[i])) {
                        commonPrefixLen = i + 1;
                    } else {
                        break;
                    }
                }
                if (commonPrefixLen > 0) {
                    const commonIdPart = idValue.substring(0, commonPrefixLen);
                    return `${firstMatch[1]}starts-with(@id, '${commonIdPart}')${firstMatch[3]}`;
                }
            }
        }

        // 2. Fallback: Generic path-segment merging
        // Split paths into segments (e.g., ["", "", "div[1]", "a[2]"])
        const splitPaths = selectors.map(s => s.split('/'));
        const minLen = Math.min(...splitPaths.map(p => p.length));
        let common = [];

        for (let i = 0; i < minLen; i++) {
            const firstSeg = splitPaths[0][i];
            if (splitPaths.every(p => p[i] === firstSeg)) {
                common.push(firstSeg);
            } else {
                // Segments differ. Try to generalize them.
                // e.g., "a[1]" and "a[2]" becomes "a"
                // e.g., "*[name()='a'][1]" and "*[name()='a'][2]" becomes "*[name()='a']"

                // This regex finds the base part and the numerical index
                const tagRegex = /^(.*)(\[\d+\])$/; 
                const firstTagMatch = firstSeg.match(tagRegex);

                if (!firstTagMatch) {
                    // First segment doesn't end in [n] (e.g., "a" and "b"), so we can't generalize. Stop.
                    break; 
                }

                const baseTag = firstTagMatch[1]; // e.g., "a" or "*[name()='a']"

                // Check if all other segments at this level have the same base tag
                if (splitPaths.every(p => {
                    const match = p[i].match(tagRegex);
                    // They must all match the regex AND have the same base tag
                    return match && match[1] === baseTag;
                })) {
                    // All have the same base tag, so we generalize
                    common.push(baseTag);
                }

                // Stop after the first difference, as the rest of the path is unstable
                break;
            }
        }

        if (common.length === 0 || (common.length === 1 && common[0] === '')) return null;

        // Rebuild the XPath
        let mergedPath = common.join('/');

        // --- THIS IS THE FIX ---
        // Trim the trailing slash if it exists (e.g., from "//div//")
        if (mergedPath.endsWith('/') && mergedPath.length > 2) {
             mergedPath = mergedPath.substring(0, mergedPath.length - 1);
        }
        return mergedPath;
        // --- END OF FIX ---

    } else {
        // --- Original logic for merging CSS selectors (this part is fine) ---
        const generalizeSegment = (a, b) => {
            if (a === b) return a;
            let idA = a.match(/\[id\^="([a-zA-Z_-]+)"\]/);
            let idB = b.match(/\[id\^="([a-zA-Z_-]+)"\]/);
            if (idA && idB && idA[1] === idB[1]) return `[id^="${idA[1]}"]`;
            let nthRegex = /(.*):nth-of-type\(\d+\)$/;
            if (nthRegex.test(a) && nthRegex.test(b)) {
                let baseA = a.match(nthRegex)[1];
                let baseB = b.match(nthRegex)[1];
                if (baseA === baseB) return baseA;
            }
            return null;
        }

        let splitPaths = selectors.map(sel => sel.split(" > "));
        let minLen = Math.min(...splitPaths.map(p => p.length));
        let common = [];

        for (let i = 0; i < minLen; i++) {
            let segs = splitPaths.map(p => p[i]);
            let firstSeg = segs[0];
            let allSame = segs.every(s => s === firstSeg);

            if (allSame) {
                common.push(firstSeg);
            } else {
                let generalized = generalizeSegment(segs[0], segs[1]);
                for (let j = 2; j < segs.length && generalized; j++) {
                    generalized = generalizeSegment(generalized, segs[j]);
                }
                if (generalized) {
                    common.push(generalized);
                } else {
                    break;
                }
            }
        }
        return common.join(" > ");
    }
}


// --- Highlight ---
let selectorColors = {};
let visibleCategories = {};
function getRandomColor() {
    const r = Math.floor(Math.random() * 200 + 30);
    const g = Math.floor(Math.random() * 200 + 30);
    const b = Math.floor(Math.random() * 200 + 30);
    return `rgba(${r},${g},${b},0.3)`;
}
function highlightAllSelectors(selectors) {
    // Clear previous highlights from page elements
    document.querySelectorAll('.__scraper_highlight').forEach(el => {
        el.classList.remove('__scraper_highlight');
        el.style.outline = '';
        el.style.backgroundColor = '';
        el.removeAttribute("data-scraper-cat");
        el.removeAttribute("title");
    });

    // --- NEW BILINGUAL HIGHLIGHTING LOGIC ---
    for (const [category, selector] of Object.entries(selectors)) {
        if (!selector) continue; // Skip if selector is null/empty

        if (!selectorColors[selector]) selectorColors[selector] = getRandomColor();
        let color = selectorColors[selector];
        let elements = [];

        try {
            // Check if it's an XPath selector
            if (selector.startsWith('/') || selector.startsWith('(')) {
                const result = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_ITERATOR_TYPE, null);
                let node = result.iterateNext();
                while (node) {
                    elements.push(node);
                    node = result.iterateNext();
                }
            } else { // Otherwise, assume it's a CSS selector
                elements = Array.from(document.querySelectorAll(selector));
            }
        } catch (e) {
            console.error(`Invalid selector for category "${category}":`, selector, e);
            continue; // Skip to the next selector
        }

        elements.forEach(el => {
            el.classList.add('__scraper_highlight');
            el.style.outline = `2px solid ${color.replace('0.3','1').replace('rgba','rgb')}`;
            el.style.backgroundColor = color;
            el.setAttribute("data-scraper-cat", category);
            el.setAttribute("title", `${category}: ${selector}`);
        });
    }
}

function applyHighlightsFromCategorySelections() {
    // Build a mapping of category -> selector for categories that are visible
    const selectorsToShow = {};
    for (const [cat, arr] of Object.entries(categorySelections || {})) {
        const isVisible = (visibleCategories[cat] !== false); // default true
        if (!isVisible) continue;
        let sel = Array.isArray(arr) ? getCommonSelector(arr) : arr;
        if (sel) selectorsToShow[cat] = sel;
    }
    highlightAllSelectors(selectorsToShow);
}

// --- Filter out media-only blocks ---
function isTextual(el) {
    // Only reject if the selected node itself is media
    const mediaTags = ['IFRAME','IMG','VIDEO','AUDIO'];
    return !mediaTags.includes(el.tagName);
}




// ----------------------------
// TOP NAVIGATION BAR (New Section)
// ----------------------------
function createTopBar() {
    if (document.getElementById("__scraper_nav_bar")) return;

    const navBar = document.createElement("div");
    navBar.id = "__scraper_nav_bar";
    Object.assign(navBar.style, {
        position: "fixed",
        top: "0",
        left: "0",
        width: "100%",
        height: "44px",
        background: "#ffffff",
        boxShadow: "0 2px 4px rgba(0,0,0,0.1)",
        zIndex: "2147483647",
        display: "flex",
        alignItems: "center",
        padding: "0 8px",
        boxSizing: "border-box",
        fontFamily: "sans-serif",
        transition: "padding-right 0.3s ease",
        paddingRight: "340px"
    });

    document.body.prepend(navBar);

    // 1. Back Button
    const backBtn = document.createElement("button"); backBtn.textContent = "◀";
    Object.assign(backBtn.style, { cursor: "pointer", border: "1px solid #ddd", background: "#f8f8f8", padding: "6px 10px", borderRadius: "4px", marginRight: "4px", fontSize: "14px" });
    backBtn.addEventListener("click", () => { safeCall("go_back").then(result => { if (result && result.url) window.location.href = result.url; }); });
    navBar.appendChild(backBtn);

    // 2. Forward Button
    const forwardBtn = document.createElement("button"); forwardBtn.textContent = "▶";
    Object.assign(forwardBtn.style, { cursor: "pointer", border: "1px solid #ddd", background: "#f8f8f8", padding: "6px 10px", borderRadius: "4px", marginRight: "8px", fontSize: "14px" });
    forwardBtn.addEventListener("click", () => { safeCall("go_forward").then(result => { if (result && result.url) window.location.href = result.url; }); });
    navBar.appendChild(forwardBtn);

    // 3. Address Bar Input
    const addressBar = document.createElement("input"); addressBar.id = "__scraper_address_input"; addressBar.type = "text"; addressBar.value = window.location.href;
    Object.assign(addressBar.style, { flexGrow: "1", padding: "6px 10px", border: "1px solid #ccc", borderRadius: "4px", marginRight: "8px", fontSize: "14px" });
    addressBar.addEventListener("keydown", (e) => { if (e.key === "Enter") { safeCall("navigate_to", addressBar.value).then(result => { if (result && result.url) window.location.href = result.url; }); e.preventDefault(); } });
    navBar.appendChild(addressBar);

    // 4. Go Button
    const goBtn = document.createElement("button"); goBtn.textContent = "Go";
    Object.assign(goBtn.style, { cursor: "pointer", border: "none", background: "#2563eb", color: "white", padding: "6px 12px", borderRadius: "4px", fontSize: "14px", fontWeight: "500" });
    goBtn.addEventListener("click", () => { safeCall("navigate_to", addressBar.value).then(result => { if (result && result.url) window.location.href = result.url; }); });
    navBar.appendChild(goBtn);

    // [NEW] 5. Docking Background Patch
    const dockBg = document.createElement("div");
    dockBg.id = "__scraper_nav_dock_bg";
    Object.assign(dockBg.style, {
        position: "absolute", top: "0", right: "0",
        height: "100%", width: "320px", // Should match panel's default width
        background: "#2563eb", // Should match panel's header color      
        zIndex: "-1", // Sits behind the navbar content
        transition: "opacity 0.3s ease",
        opacity: "1" // Initially visible
    });
    navBar.appendChild(dockBg);

    document.body.prepend(navBar);

    function getFixedHeadersHeight() {
        const headers = Array.from(document.querySelectorAll("header, nav, [role='banner']"))
            .filter(el => el.offsetHeight > 0 && getComputedStyle(el).position === "fixed");
        return headers.reduce((sum, el) => sum + el.offsetHeight, 0);
    }

    function adjustContentMargin() {
    const totalHeaderHeight = navBar.offsetHeight ;

    const bodyChildren = Array.from(document.body.children).filter(c =>
        c !== navBar &&
        !c.id.startsWith("__scraper_")
    );

    bodyChildren.forEach(el => {
        // Apply exact margin to push content below navbar + fixed headers
        el.style.marginTop = totalHeaderHeight + "px";
    });
}

    adjustContentMargin();

    // Re-adjust on window resize (headers may resize)
    window.addEventListener("resize", adjustContentMargin);
    window.addEventListener("load", adjustContentMargin);
}

function updateAddressBar(url) {
    const addressBar = document.getElementById("__scraper_address_input");
    if (addressBar) addressBar.value = url;
}



// ... (Near the end of the js_code initialization block) ...
if (!window.__scraper_functions_initialized) {
    // ... (rest of the initialization block)

    // --- CALL THE NEW FUNCTION ---
    createTopBar(); 

    // Override original location tracking to update the address bar
    window.addEventListener('load', () => {
        // This is primarily for single-page application navigation or hash changes
        updateAddressBar(window.location.href); 
    });
}
// ... (rest of the js_code execution block) ...
if (!document.getElementById("__scraper_panel")) {
    createTopBar(); // Ensure it runs even if functions were already initialized
    createSidePanel();
}
updateAddressBar(window.location.href); // Run on every injection

// --- Side panel ---
// ----------------------------
// Modernized side panel + list
// ----------------------------
function createSidePanel() {
  if (document.getElementById("__scraper_panel")) return;

  // --- [NEW] Docking Helper Functions ---
  function undockPanel() {
    isDocked = false;
    const panel = document.getElementById("__scraper_panel");
    const navBar = document.getElementById("__scraper_nav_bar"); 
    const dockBg = document.getElementById("__scraper_nav_dock_bg");
    const header = panel ? panel.querySelector("div[style*='cursor']") : null;
    const toggleWrap = document.getElementById("__scraper_pinpoint_toggle");
    if (!toggleWrap) createFloatingPinpointToggle();



    updateHeaderPadding();

    if (navBar) navBar.style.paddingRight = "8px";
    toggleWrap.style.top = "68px"

    if (dockBg) dockBg.style.opacity = "0";
    if (resizeHandle) resizeHandle.style.display = "block";

    if (header) header.style.cursor = "ns-resize",
    panel.style.transform = "translateY(40px)";
    savePanelState();
}

  function dockPanel() {
    isDocked = true;
    const panel = document.getElementById("__scraper_panel");
    const navBar = document.getElementById("__scraper_nav_bar");
    const dockBg = document.getElementById("__scraper_nav_dock_bg");
    const header = panel ? panel.querySelector("div[style*='cursor']") : null;
    document.getElementById("__scraper_resize_handle").style.display = "none";
    const toggleWrap = document.getElementById("__scraper_pinpoint_toggle");
    panel.style.transform = "translateY(0px)";
    if (resizeHandle) resizeHandle.style.display = "none";
     updateHeaderPadding();
    if (navBar) navBar.style.paddingRight = "340px";
    if (dockBg) dockBg.style.opacity = "1";
    if (panel) {
        Object.assign(panel.style, {
            top: '0px', right: '0px', left: 'auto', bottom: 'auto',
        });
    }
    if (header) header.style.cursor = "default"; 
    toggleWrap.style.top = "56px"
    savePanelState();
}

  // Panel container
  const topContainer = document.createElement("div");
  topContainer.id = "__scraper_topcontainer";
  Object.assign(topContainer.style, { position: "fixed", top: "0", left: "0", width: "100%", height: "100%", zIndex: "2147483647", pointerEvents: "none" });
  document.body.appendChild(topContainer);

  // Panel element
  const panel = document.createElement("div");
  panel.id = "__scraper_panel";
  Object.assign(panel.style, {
    position: "absolute",
    top: "0px",      // MODIFIED: Start docked at the top
    right: "0px",    // MODIFIED: Start docked at the right
    width: "320px",
    height: "0px",
    background: "#f5f7fa", boxShadow: "0 8px 30px rgba(0,0,0,0.15)",
    fontFamily: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`,
    display: "flex", flexDirection: "column", pointerEvents: "auto",

    transition: "height 0.25s ease, transform 0.12s ease"
  });
  topContainer.appendChild(panel);

function createToggles() {
    if (document.getElementById("__scraper_toggles_wrap")) return;

    const togglesWrap = document.createElement("div");
    togglesWrap.id = "__scraper_toggles_wrap";
    Object.assign(togglesWrap.style, {
        position: "absolute",
        padding: "0 0px 0px 24px",
        top: "56px",
        right: "24px",
        zIndex: "2147483651",
        pointerEvents: "auto",
        userSelect: "none",
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-end",
        gap: "8px",
        fontFamily: "sans-serif",
        visibility: "hidden"
    });
    panel.appendChild(togglesWrap);

    const createToggle = (id, labelText, initialValue, callback) => {
        const toggle = document.createElement("div");
        toggle.id = id;
        Object.assign(toggle.style, {
            display: "flex", alignItems: "center", gap: "6px"
        });

        const label = document.createElement("span");
        label.textContent = labelText;
        Object.assign(label.style, { fontSize: "12px", fontWeight: "600", color: "#2563e" });

        const switchBtn = document.createElement("button");
        const knob = document.createElement("div");
        Object.assign(switchBtn.style, { width: "23px", height: "12px", borderRadius: "16px", border: "2px solid #222", backgroundColor: "#444", padding: "2px", display: "flex", alignItems: "center", cursor: "pointer", transition: "background-color 0.2s ease", boxShadow: "0 2px 6px rgba(0,0,0,0.4)" });
        Object.assign(knob.style, { width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#fff", transform: "translateX(-2px)", transition: "transform 0.2s ease" });

        switchBtn.appendChild(knob);
        toggle.appendChild(label);
        toggle.appendChild(switchBtn);

        function updateUI(on) {
            switchBtn.setAttribute("aria-pressed", on ? "true" : "false");
            switchBtn.style.backgroundColor = on ? "#4CAF50" : "#444";
            knob.style.transform = on ? "translateX(8px)" : "translateX(-2px)";
        }

        updateUI(!!initialValue);

        switchBtn.addEventListener("click", () => {
            // ---- THIS IS THE FIX ----
            // First, compare the string to "true" to get a real boolean, then invert it.
            const newValue = !(switchBtn.getAttribute("aria-pressed") === "true");

            updateUI(newValue);
            callback(newValue);
            savePanelState();
        });

        return toggle;
    };

    // 1. Create Pinpoint Toggle
    const pinpointToggle = createToggle("__scraper_pinpoint_toggle", "Match only selected element:", pinpointMode, (newValue) => {
        pinpointMode = newValue;
        lastSelectedElement = null;
    });
    togglesWrap.appendChild(pinpointToggle);

    // 2. Create Selector Mode Toggle (CSS/XPath)
    window.selectorMode = window.selectorMode || 'css';
    const selectorModeToggle = createToggle("__scraper_selector_mode_toggle", "Use XPath (on) / CSS (off):", selectorMode === 'xpath', (isXPath) => {
        window.selectorMode = isXPath ? 'xpath' : 'css';
        lastSelectedElement = null;
    });
    togglesWrap.appendChild(selectorModeToggle);
}


createToggles();
  function updateHeaderPadding() {
    const header = document.querySelector("#__scraper_panel div[style*='cursor']");
    if (!header) return;

    if (isCollapsed) {
        header.style.paddingTop = "2px";
        header.style.paddingBottom = "2px";
        header.style.height = "32px"; // optional: fix collapsed height
    } else {
        header.style.paddingTop = "6px";
        header.style.paddingBottom = "6px";
        header.style.height = ""; // reset
    }

    const resizeHandle = document.getElementById("__scraper_resize_handle");



    if (resizeHandle) resizeHandle.style.display = isDocked || isCollapsed ? "none" : "block";
}

  // Header
  const header = document.createElement("div");
  Object.assign(header.style, { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 22px", cursor: "move", background: "#2563eb", color: "white", });
  panel.appendChild(header);

  // Title
  const title = document.createElement("h3");
  title.textContent = "Template Builder";
  Object.assign(title.style, { margin: "0", fontSize: "13px", fontWeight: "600", color: "white",lineHeight: "16px",  });
  header.appendChild(title);

  // [NEW] Header Buttons Container
  const headerButtons = document.createElement("div");
  Object.assign(headerButtons.style, { display: "flex", alignItems: "center" });
  header.appendChild(headerButtons);


  // [NEW] Dock / Undock Button with overlapping squares
    const dockBtn = document.createElement("button");
    dockBtn.setAttribute("aria-label", "Dock / Undock");
    Object.assign(dockBtn.style, {
        cursor: "pointer",
        background: "transparent",
        border: "none",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "4px 20px"
    });

    // Inline SVG: two overlapping squares
    dockBtn.innerHTML = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="4" y="4" width="12" height="12" rx="2" ry="2"></rect>
      <rect x="8" y="8" width="12" height="12" rx="2" ry="2"></rect>
    </svg>
    `;

    // Click handler toggles dock state
    dockBtn.addEventListener("click", () => {
        if (isDocked) {
            undockPanel();
        } else {
            dockPanel();
        }

    });

    headerButtons.appendChild(dockBtn);


      // Collapse toggle button
      const toggleBtn = document.createElement("button");
      toggleBtn.innerHTML = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" 
         stroke-linecap="round" stroke-linejoin="round">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
    `;
      Object.assign(toggleBtn.style, { cursor: "pointer", border: "none", background: "transparent", color: "white", fontSize: "16px", padding: "4px 10px" });
      headerButtons.appendChild(toggleBtn);

    // Collapse / expand logic
    let isCollapsed = true;
    let panelHeight = 600; 
    toggleBtn.addEventListener("click", () => {
    const header = panel.querySelector("div[style*='cursor']");
    const toggleWrap = document.getElementById("__scraper_toggles_wrap");
    isCollapsed = !isCollapsed;
    // Apply the visual state based on the new logical state
    if (isCollapsed) {
        // COLLAPSED state
        panel.style.height = "0px";
        list.style.display = "none";
        btnContainer.style.display = "none";
        addBtn.style.display = "none";
        if (toggleWrap) {
            toggleWrap.style.visibility = "hidden"; // <- use visibility instead of display
            toggleWrap.style.pointerEvents = "none"; // make it non-clickable
        }

        // Rotate SVG arrow UP (collapsed)
        const svgArrow = toggleBtn.querySelector("svg");
        if (svgArrow) svgArrow.style.transform = "rotate(180deg)"; // Arrow up/closed
    } else {
        // EXPANDED state
        panel.style.height = panelHeight + "px"; // Use the saved/default height
        list.style.display = "flex";
        btnContainer.style.display = "flex";
        addBtn.style.display = "flex";
           if (toggleWrap) {
            toggleWrap.style.visibility = "visible"; // show again
            toggleWrap.style.pointerEvents = "auto";
        }


        // Rotate SVG arrow DOWN (expanded)
        const svgArrow = toggleBtn.querySelector("svg");
        if (svgArrow) svgArrow.style.transform = "rotate(0deg)"; // Arrow down/open
    }

    panel.style.height = isCollapsed ? "0px" : "600px";
    list.style.display = isCollapsed ? "none" : "flex";
    btnContainer.style.display = isCollapsed ? "none" : "flex";
    addBtn.style.display = isCollapsed ? "none" : "flex";

    // Rotate SVG arrow
    const svgArrow = toggleBtn.querySelector("svg");
    if (svgArrow) {
        svgArrow.style.transition = "transform 0.2s ease";
        svgArrow.style.transform = isCollapsed ? "rotate(-90deg)" : "rotate(0deg)";
    }

    updateHeaderPadding();
    savePanelState();
    });


    // Resize handle (above header)
    const resizeHandle = document.createElement("div");
    resizeHandle.id = "__scraper_resize_handle";  // Add this
    Object.assign(resizeHandle.style, {
        width: "100%",
        height: "8px",
        cursor: "ns-resize",
        background: "#2563eb",
        flex: "0 0 8px",
        position: "relative", 
        zIndex: "100",    
        pointerEvents: "all"    
    });
panel.insertBefore(resizeHandle, header);

    // Selector list (scrolling area)
  const list = document.createElement("ul");
  list.id = "__scraper_list";
  Object.assign(list.style, {
    listStyle: "none",
    padding: "0 12px 12px 12px", // Adjusted padding
    marginTop: "60px",
    marginBottom: "80px",
    flex: "1 1 auto",
    display: "flex",             // Use flexbox
    flexDirection: "column",     // Stack items vertically
    overflowY: "auto",
    overflowX: "hidden",
    gap: "8px",                  // This will now correctly space the pills
    boxSizing: "border-box"
  });
  panel.appendChild(list);

  // Bottom button bar (fixed to panel bottom)
  const btnContainer = document.createElement("div");
  btnContainer.id = "__scraper_buttons";
  Object.assign(btnContainer.style, {
    display: "flex",
    gap: "8px",
    padding: "10px",
    borderTop: "1px solid #e6eefc",
    alignItems: "center",
    height: "56px",
    boxSizing: "border-box",
    position: "absolute",
    left: "0",
    right: "0",
    bottom: "0",
    background: "#f5f7fa"
  });
  panel.appendChild(btnContainer);

  // style helper (defined BEFORE any button use)
   const styleButton = (btn, primary = false, compact = false) => {
    Object.assign(btn.style, {
      // Base Styles
      border: "1px solid transparent", // Use a small border for stability
      padding: compact ? "6px 10px" : "10px 16px",
      borderRadius: "10px", // Increased radius
      fontSize: "13px",
      fontWeight: "500", // Medium weight
      transition: "all 0.2s ease",
      cursor: "pointer",
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      gap: "6px",
      outline: "none",
      boxShadow: "0 1px 3px rgba(0,0,0,0.05)" // Light default shadow
    });

    if (primary) {
      // Primary (Save / Add)
      btn.style.background = "#2563eb";
      btn.style.color = "white";
      btn.style.boxShadow = "0 4px 12px rgba(37,99,235,0.2)"; // Stronger shadow
    } else {
      // Secondary (Load / Nav)
      btn.style.background = "#f0f4f9";
      btn.style.color = "#0f172a";
      btn.style.borderColor = "#e6eefc";
    }

    // Hover Effects
    btn.onmouseenter = () => {
        if (primary) {
             btn.style.background = "#1d4ed8"; // Slightly darker blue
             btn.style.transform = "scale(1.02)";
        } else {
             btn.style.background = "#e4eaf2"; // Slightly darker gray
             btn.style.transform = "scale(1.02)";
        }
    };
    btn.onmouseleave = () => {
        if (primary) {
            btn.style.background = "#2563eb";
            btn.style.transform = "scale(1)";
        } else {
            btn.style.background = "#f0f4f9";
            btn.style.transform = "scale(1)";
        }
    };
  };

  // Buttons (Load, Save, Prev, Next)
  const loadBtn = document.createElement("button"); loadBtn.textContent = "📁 Load"; styleButton(loadBtn, false);
  const saveBtn = document.createElement("button"); saveBtn.textContent = "💾 Save"; styleButton(saveBtn, true);
  const spacer = document.createElement("div"); spacer.style.flex = "1";
  const prevBtn = document.createElement("button"); prevBtn.textContent = "<"; styleButton(prevBtn, false, true);
  const nextBtn = document.createElement("button"); nextBtn.textContent = ">"; styleButton(nextBtn, false, true);

loadBtn.style.width = "100%";
saveBtn.style.width = "100%";

  btnContainer.appendChild(loadBtn);
  btnContainer.appendChild(saveBtn);



  // Floating circular Add button (above bottom bar)
  const addBtn = document.createElement("button");
  addBtn.textContent = "+";
  addBtn.id = "__scraper_add_btn";              // <-- stable id
  addBtn.dataset.role = "floating-add";
  styleButton(addBtn, true);
  Object.assign(addBtn.style, {
  width: "44px",
  height: "44px",
  borderRadius: "50%",
  position: "absolute",
  right: "16px",
  bottom: "calc(56px + 12px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: "40px",    // bigger plus
  fontFamily: "Arial, sans-serif",
  color: "white",      // force white
  boxShadow: "0 6px 18px rgba(37,99,235,0.18)"
});
  panel.appendChild(addBtn);

   /// Drag logic (header)
let isDragging = false,
    dragOffsetX = 0,
    dragOffsetY = 0;

header.addEventListener("mousedown", function(e) {
    // Ignore clicks on buttons or when docked
    if (isDocked || e.target.tagName === 'BUTTON' || e.target.closest('button')) return;

    isDragging = true;
    const panelRect = panel.getBoundingClientRect();

    // --- THIS IS THE FIX ---
    // Calculate the offset from the top-left corner of the panel's
    // actual rendered position, which includes the transform.
    dragOffsetX = e.clientX - panelRect.left;
    dragOffsetY = e.clientY - panelRect.top;

    // Prevent text selection while dragging
    document.body.style.userSelect = "none";
    e.preventDefault();
});

document.addEventListener("mousemove", function(e) {
    if (isDragging) {
        // When setting the new position, use the calculated offsets
        panel.style.left = e.clientX - dragOffsetX + "px";
        panel.style.top = e.clientY - dragOffsetY + "px";
        panel.style.transform = "none"; // Clear the transform after the first move
        panel.style.bottom = "auto";
        panel.style.right = "auto";
    }
});

document.addEventListener("mouseup", function() {
    if (isDragging) {
        isDragging = false;
        document.body.style.userSelect = "auto";
        savePanelState();
    }
});
  document.addEventListener("mouseup", function () {
    if (isDragging) isDragging = false;
    document.body.style.userSelect = "auto";
    savePanelState();
  });
    panel.style.height = "0px";
    list.style.display = "none";
    btnContainer.style.display = "none";
    addBtn.style.display = "none";
    resizeHandle.style.display = "none";


   restorePanelState();

  // Resize logic (resizeHandle)
  let isResizing = false, startY = 0, startHeight = 0;
  let startTop = 0; 
  let rAF_ID = null; // Variable to hold the requestAnimationFrame ID

  // Function to handle the actual DOM update for smoother resizing
  const updatePanelSize = (e) => {
    if (!isResizing) return;

    let dy = startY - e.clientY;
    let newHeight = Math.max(220, startHeight + dy);
    panel.style.height = newHeight + "px";
    panel.style.top = startTop - (newHeight - startHeight) + "px";

    panelHeight = newHeight;  // save logical height
};


  function savePanelState() {
        const panel = document.getElementById("__scraper_panel");
        if (!panel) return;
        const rect = panel.getBoundingClientRect();


        const normalizedSelectors = {};
        for (const [cat, arr] of Object.entries(categorySelections || {})) {
            if (Array.isArray(arr)) {
                normalizedSelectors[cat] = getCommonSelector(arr) || arr[0];
            } else if (typeof arr === 'string') {
                normalizedSelectors[cat] = arr;
            } else {
                normalizedSelectors[cat] = null;
            }
        }
        const visibility = {};
        for (const cat of Object.keys(normalizedSelectors)) {
            visibility[cat] = (visibleCategories[cat] !== false);
        }

        const state = {
            isDocked: isDocked,
            left: isDocked ? null : panel.style.left,
            top: isDocked ? null : panel.style.top,
            width: panel.style.width || rect.width + "px",
            height: panel.style.height || rect.height + "px",
            collapsed: !!isCollapsed,
            selectors: normalizedSelectors, 
            visible: visibility,         
            selectorColors: selectorColors,
            pinpointMode: !!pinpointMode,
            selectorMode: window.selectorMode || 'css'
        };
        localStorage.setItem("__scraper_panel_state", JSON.stringify(state));
    }



  // Mousedown: Start the resize and prepare initial values
  resizeHandle.addEventListener("mousedown", function (e) {
    if (isCollapsed) {
        isCollapsed = false;
        panel.style.height = panelHeight + "px"; // restore logical height
        list.style.display = "flex";
        btnContainer.style.display = "flex";
        addBtn.style.display = "flex";
    }

    undockPanel();
    isResizing = true;
    startY = e.clientY;
    startHeight = panel.offsetHeight;
    startTop = parseFloat(panel.style.top) || (window.innerHeight - startHeight);
    e.preventDefault();
    });




  // Mousemove: Only track the mouse position, do NOT update DOM here
  document.addEventListener("mousemove", function (e) {
    if (isResizing && !rAF_ID) {
        // Start the requestAnimationFrame loop only once
        rAF_ID = requestAnimationFrame(() => updatePanelSize(e));
    } else if (isResizing) {
        // When loop is running, simply update the event object for the next frame
        // This relies on the browser environment implicitly passing the latest event state, 
        // but for safety, the simplest approach is to call the update function directly 
        // with the latest event and let rAF handle the scheduling.
        // A cleaner implementation often involves storing the latest mouse coordinates 
        // outside the handler, but this minimal change should suffice:
        if (rAF_ID) {
            cancelAnimationFrame(rAF_ID);
        }
        rAF_ID = requestAnimationFrame(() => updatePanelSize(e));
    }
  });

  // Mouseup: Stop the resize and cancel the animation loop
  document.addEventListener("mouseup", function () {
    if (isDragging || isResizing) {
        isDragging = false;
        isResizing = false;
        document.body.style.userSelect = "auto";
        if (rAF_ID) { // from your resize logic
            cancelAnimationFrame(rAF_ID);
            rAF_ID = null;
        }
        savePanelState();
    }
});
addBtn.addEventListener("click", () => {
    if (!lastSelectedElement) { alert("No element selected!"); return; }
    
    let category = prompt(`Selected text: "${lastSelectedElement.selectedText}"\nEnter category name (Cancel to skip):`);
    if (!category) return;

    const newSelector = lastSelectedElement.selector;
    const isXPath = newSelector.startsWith('/') || newSelector.startsWith('(');

    // Determine the base category name (without trailing '-')
    const isExclusionAttempt = category.endsWith('-');
    const baseCategory = isExclusionAttempt ? category.slice(0, -1) : category;
    
    // Get the current merged selector for the base category
    const existingSelector = (categorySelections[baseCategory] && categorySelections[baseCategory].length > 0) 
                             ? getCommonSelector(categorySelections[baseCategory]) 
                             : null;

    // --- CORRECTED EXCLUSION LOGIC (XPath Only) ---
    if (isXPath && isExclusionAttempt && existingSelector) {
        
        let newElement = null;
        try {
            const result = document.evaluate(newSelector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            newElement = result.singleNodeValue;
        } catch (e) {
            console.error("Invalid new selector for exclusion check:", newSelector, e);
        }

        if (newElement) {
            category = baseCategory; 
            const exclusionPredicate = getExclusionPredicate(newElement);
            let newExcludedSelector = existingSelector; 
            
            // Regex to match the LATEST exclusion filter, capturing the tag and the content
            // Captures: 1: Target Tag (e.g., /*, /a, or nothing) 2: Exclusion Content
            const exclusionRegex = /(\/\*|\[\w+\])?\[not\((.*)\)\]$/;
            const existingExclusionMatch = existingSelector.match(exclusionRegex);

            if (existingExclusionMatch) {
                 const filterMatch = existingExclusionMatch[0];
                 const currentExclusions = existingExclusionMatch[2]; // Content inside not()
                 const baseSelector = existingSelector.substring(0, existingSelector.length - filterMatch.length);
                 const targetTag = existingExclusionMatch[1] || '/*';

                 const newExclusionContentMatch = exclusionPredicate.match(/not\((.*)\)/);
                 if (!newExclusionContentMatch) {
                    alert("Could not extract exclusion content for the new element. Exclusion not applied.");
                    return;
                 }
                 const newExclusionContent = newExclusionContentMatch[1];
                 
                 // Prevent duplicates
                 if (currentExclusions.includes(newExclusionContent)) {
                    alert(`Selector for '${category}' already excludes this element.`);
                    return;
                 }

                 // Combine them using 'or' into a single [not(...)] segment
                 const newExclusionFilter = `${targetTag}[not(${currentExclusions} or ${newExclusionContent})]`;

                 newExcludedSelector = baseSelector + newExclusionFilter;
                 
            } else {
                 // No existing exclusion, add the first one as a child exclusion
                 newExcludedSelector = `${existingSelector}/*[${exclusionPredicate.substring(1)}`;
            }

            // Save the newly modified selector and update the UI
            safeCall("save_selector", category, newExcludedSelector).then(() => {
                categorySelections[category] = [newExcludedSelector]; 
                addSelectorToPanel(category, newExcludedSelector);
                applyHighlightsFromCategorySelections();
                savePanelState();
            });
            lastSelectedElement = null;
            return; // EXIT: Exclusion handled.
        }
    }
    // --- END CORRECTED EXCLUSION LOGIC ---
    
    // Normal selector logic (merging or adding first selector)
    
    // Check if the user is trying to add a new selector to an existing category (non-exclusion case).
    if (existingSelector && existingSelector !== newSelector) {
         categorySelections[baseCategory] = []; // Reset for merging
    }

    if (!categorySelections[baseCategory])
        categorySelections[baseCategory] = [];
    
    if (categorySelections[baseCategory].includes(newSelector)) {
        console.log("Duplicate selector, skipping.");
        return;
    }

    // 1. Temporarily add the new selector to the list
    categorySelections[baseCategory].push(newSelector);
    
    // 2. Try to merge all selectors in the list
    let mergedSelector = getCommonSelector(categorySelections[baseCategory]);
    
    if (mergedSelector) {
        // 3. SUCCESS: The merge worked. Save and update the pill.
        safeCall("save_selector", baseCategory, mergedSelector).then(() => {
            categorySelections[baseCategory] = [mergedSelector];
            addSelectorToPanel(baseCategory, mergedSelector);
            applyHighlightsFromCategorySelections();
            savePanelState();
        });
    } else {
        // 4. FAILURE: The merge failed.
        categorySelections[baseCategory].pop(); 
        
        if (categorySelections[baseCategory].length > 0) {
            alert("Selection failed. The new item is too different to be merged with the existing items in this category. Selection not added.");
        } else {
            alert("Selection failed. Could not generate a valid selector for this item.");
        }
    }
    
    lastSelectedElement = null;
});
// --- NEW: Helper to build an exclusion predicate 
function getExclusionPredicate(el) {
    // Priority 1: Check for unique ID (still best to use exact match for ID)
    if (el.id) {
        const prefix = el.id.replace(/\d+$/, '');
        if (prefix.length > 2) {
            return `[not(starts-with(@id, '${prefix}'))]`;
        }
        return `[not(@id='${el.id}')]`;
    }

    // Priority 2: Use a combination of tag and class (UPDATED TO USE CONTAINS)
    if (el.className) {
        // Select the first "good" class for exclusion, or use the entire className string.
        // Using the entire className string is the most precise exclusion.
        const classToExclude = el.className.trim();

        if (classToExclude.length > 0) {
            // Use 'contains' to match the specific class string within the element's @class attribute.
            // Note: For multi-class matches, you might eventually need normalize-space(), but contains() is often sufficient 
            // for simple exclusions like the example you provided (e.g., alignfull).
            return `[not(descendant-or-self::*[contains(@class, '${classToExclude}')])]`;
        }
    }

    // Priority 3: Fallback to tag and unique position (pinpoint logic, simple version)
    // This is less stable but necessary for unclassed/un-ID'd elements.
    let index = 1;
    let sibling = el.previousElementSibling;
    while (sibling) {
        if (sibling.tagName === el.tagName) {
            index++;
        }
        sibling = sibling.previousElementSibling;
    }
    const tagName = el.tagName.toLowerCase();
    return `[not(*[name()='${tagName}'][${index}])]`; 
}

    loadBtn.addEventListener("click",()=>{ safeCall("load_template").then(result=>{
        if(result.status==="ok"){ list.innerHTML=""; categorySelections={}; for(const [category,selector] of Object.entries(result.selectors)){ addSelectorToPanel(category,selector); categorySelections[category]=[selector]; } highlightAllSelectors(result.selectors); alert("Template loaded from "+result.filename); } else alert("Error: "+result.message); }); });

    saveBtn.addEventListener("click",()=>{ let filename=prompt("Enter filename for template:","template.json"); if(!filename) return; safeCall("save_template", filename).then(result=>alert("Template saved to "+result.filename)); });



    prevBtn.addEventListener("click", () => {
        safeCall("go_back").then(result => {
            if (result && result.url) {
                // Let JavaScript handle the navigation
                window.location.href = result.url;
            }
        });
    });

    nextBtn.addEventListener("click", () => {
        safeCall("go_forward").then(result => {
            if (result && result.url) {
                // Let JavaScript handle the navigation
                window.location.href = result.url;
            }
        });
    });


  setInterval(() => { topContainer.style.zIndex = "2147483647"; }, 100);

// --- REPLACE your entire restorePanelState function with this one ---
function restorePanelState() {
    const saved = localStorage.getItem("__scraper_panel_state");
    const panel = document.getElementById("__scraper_panel");
    if (!panel || !saved) return;

    // Use the correct container ID here
    let togglesWrap = document.getElementById("__scraper_toggles_wrap"); 
    if (!togglesWrap) {
        createToggles();
        togglesWrap = document.getElementById("__scraper_toggles_wrap");
    }

    const listEl = document.getElementById("__scraper_list");
    const btnContainerEl = document.getElementById("__scraper_buttons");
    const addBtnEl = document.getElementById("__scraper_add_btn");

    try {
        const state = JSON.parse(saved);

        // --- Dock / Undock ---
        isDocked = state.isDocked === true;
        if (isDocked) {
            dockPanel();
        } else {
            undockPanel();
            if (state.left) panel.style.left = state.left;
            if (state.top) panel.style.top = state.top;
        }

        // --- Collapse / Expand ---
        isCollapsed = state.collapsed === true;
        panelHeight = parseFloat(state.height) || 600;

        if (isCollapsed) {
            panel.style.height = "0px";
            listEl.style.display = "none";
            btnContainerEl.style.display = "none";
            addBtnEl.style.display = "none";
            if (togglesWrap) {
                togglesWrap.style.visibility = "hidden";
                togglesWrap.style.pointerEvents = "none";
            }
        } else {
            panel.style.height = panelHeight + "px";
            listEl.style.display = "flex";
            btnContainerEl.style.display = "flex";
            addBtnEl.style.display = "flex";
            if (togglesWrap) {
                togglesWrap.style.visibility = "visible";
                togglesWrap.style.pointerEvents = "auto";
                togglesWrap.style.top = isDocked ? "56px" : "68px";
            }
        }

        // --- UNIFIED LOGIC to update toggles AFTER handling visibility ---
        pinpointMode = !!state.pinpointMode;
        const pinpointToggle = document.querySelector("#__scraper_pinpoint_toggle button");
        if (pinpointToggle) {
            const on = pinpointMode;
            const knob = pinpointToggle.querySelector("div");
            pinpointToggle.setAttribute("aria-pressed", on ? "true" : "false");
            pinpointToggle.style.backgroundColor = on ? "#4CAF50" : "#444";
            knob.style.transform = on ? "translateX(8px)" : "translateX(-2px)";
        }

        window.selectorMode = state.selectorMode || 'css';
        const selectorModeToggle = document.querySelector("#__scraper_selector_mode_toggle button");
        if (selectorModeToggle) {
            const on = window.selectorMode === 'xpath';
            const knob = selectorModeToggle.querySelector("div");
            selectorModeToggle.setAttribute("aria-pressed", on ? "true" : "false");
            selectorModeToggle.style.backgroundColor = on ? "#4CAF50" : "#444";
            knob.style.transform = on ? "translateX(8px)" : "translateX(-2px)";
        }

        updateHeaderPadding();

        // --- Restore selectors / list items ---
        selectorColors = state.selectorColors || {};
        listEl.innerHTML = "";
        categorySelections = {};
        visibleCategories = state.visible || {};
        const selectorsToHighlight = {};
        for (const [category, selector] of Object.entries(state.selectors || {})) {
            categorySelections[category] = [selector];
            if (typeof addSelectorToPanel === 'function') {
                addSelectorToPanel(category, selector);
            }
            if (visibleCategories[category] !== false) {
                selectorsToHighlight[category] = selector;
            }
        }
        highlightAllSelectors(selectorsToHighlight);

    } catch (e) {
        console.error("Failed to restore panel state:", e);
    }
}




}


// Improved pill-style selector entry (replacement)
// --- REPLACE your entire addSelectorToPanel function with this corrected version ---

function addSelectorToPanel(category, selector, initialVisible = true) {
  const list = document.getElementById("__scraper_list");
  if (!list) return;

  // Remove existing entry for the category (if any)
  Array.from(list.children).forEach(ch => { if (ch.dataset && ch.dataset.cat === category) ch.remove(); });

  const li = document.createElement("li");
  li.dataset.cat = category;
  Object.assign(li.style, {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    margin: "6px 6px",
    padding: "6px 12px",
    borderRadius: "12px",
    border: "1px solid #e6eefc",
    boxShadow: "0 2px 8px rgba(16,24,40,0.03)",
    fontSize: "13px",
    width: "100%",
    boxSizing: "border-box",
    minHeight: "44px",
    height: "44px",
    overflow: "hidden"
  });

  // --- 1. SET THE BACKGROUND COLOR BASED ON SELECTOR TYPE ---
  const isXPath = selector.startsWith('/') || selector.startsWith('(');
  if (isXPath) {
    li.style.background = "#1d9f70"; // Green for XPath
    li.style.borderColor = "#059669";
  } else {
    li.style.background = "#4977dc"; // Blue for CSS
  }
  li.style.color = "white"; // White text for both

  // Left: checkbox + swatch container
  const left = document.createElement("div");
  Object.assign(left.style, { display: "flex", alignItems: "center", gap: "8px", flex: "0 0 auto" });

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = (typeof visibleCategories[category] === "undefined") ? !!initialVisible : !!visibleCategories[category];
  Object.assign(checkbox.style, { width: "16px", height: "16px", cursor: "pointer" });

  const swatch = document.createElement("span");
  swatch.title = "Highlight color";
  Object.assign(swatch.style, {
    width: "16px",
    height: "16px",
    borderRadius: "4px",
    border: "1px solid rgba(0,0,0,0.08)",
    display: "inline-block",
    verticalAlign: "middle"
  });

  if (!selectorColors[selector]) selectorColors[selector] = getRandomColor();
  let color = selectorColors[selector];
  swatch.style.backgroundColor = color.replace(/0\.3\)$/,'0.8)');

  // --- 2. THE OLD HARDCODED LINE HAS BEEN REMOVED FROM HERE ---

  checkbox.style.accentColor = color.replace(/0\.3\)$/,'1)');

  left.appendChild(checkbox);
  left.appendChild(swatch);

  // (The rest of the function continues as normal)
  const text = document.createElement("span");
  text.textContent = category + " →";
  text.style.fontWeight = "600";
  Object.assign(text.style, { flex: "0 0 auto", whiteSpace: "nowrap" });

  const sel = document.createElement("code");
  sel.textContent = selector;
  sel.title = selector;
  Object.assign(sel.style, {
    fontFamily: "monospace",
    fontSize: "12px",
    opacity: "0.75",
    marginLeft: "6px",
    maxWidth: "100%",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    display: "inline-block",
    verticalAlign: "middle",
    flex: "1 1 0%",
    minWidth: "0"
  });

  const del = document.createElement("button");
  del.textContent = "✕";
  Object.assign(del.style, {
    marginLeft: "8px",
    border: "none",
    background: "transparent",
    cursor: "pointer",
    fontSize: "14px",
    flex: "0 0 auto",
    color: "white"
  });

  del.onmouseenter = () => { del.style.color = "#f0f0f0"; };
  del.onmouseleave = () => { del.style.color = "white"; };
  del.addEventListener("click", () => {
    pywebview.api.remove_selector(category).then(() => {
      li.remove();
      delete categorySelections[category];
      delete visibleCategories[category];
      pywebview.api.get_all_selectors().then(allSelectors => {
        applyHighlightsFromCategorySelections();
      });
      savePanelState();
    });
  });

  checkbox.addEventListener("change", () => {
    visibleCategories[category] = checkbox.checked;
    applyHighlightsFromCategorySelections();
    savePanelState();
  });

  swatch.addEventListener("click", () => {
    checkbox.checked = !checkbox.checked;
    checkbox.dispatchEvent(new Event('change'));
  });

  li.appendChild(left);
  li.appendChild(text);
  li.appendChild(sel);
  li.appendChild(del);
  list.appendChild(li);

  if (!categorySelections[category]) categorySelections[category] = [selector];
}
// --- Element Selection ---
document.addEventListener('mousedown', function(e) {
    // Only capture on left-click and if not interacting with the panel/navbar
    if (e.button !== 0) return;
    if (e.target.closest('#__scraper_topcontainer')) return; 
    if (e.target.closest('#__scraper_nav_bar')) return;

    let container = e.target;
    if (container.closest('iframe')) return;
    if (!isTextual(container)) return; 

    // Determine the selector based on current mode
    let selector;
    if (window.selectorMode === 'xpath') {
        selector = pinpointMode ? getXPathPinpointSelector(container) : getXPathElasticSelector(container);
    } else { // Default to CSS
        selector = pinpointMode ? getPinpointSelector(container) : getElasticSelector(container);
    }

    if (!selector) return;

    // Temporarily store the element details. selectedText is empty for a click.
    lastSelectedElement = { selector: selector, selectedText: "" }; 

    // Visual feedback: highlight the element on click
    document.querySelectorAll('.__scraper_temp_highlight').forEach(el => {
        el.classList.remove('__scraper_temp_highlight');
        el.style.boxShadow = '';
    });

    container.classList.add('__scraper_temp_highlight');
    container.style.boxShadow = `0 0 0 3px ${selectorColors[selector] ? selectorColors[selector].replace('0.3','1').replace('rgba','rgb') : '#fbbd08'} inset`;

    e.stopPropagation(); // Stop click from propagating potentially
}, true); // Use capturing phase

// --- Text Selection (Overrides Element Selection if text is selected) ---
document.addEventListener('mouseup', function(e) {
    let selection = window.getSelection();
    let selectedText = selection.toString().trim();

    // Check if we actually made a text selection
    if (!selection || selection.isCollapsed || !selectedText) {
        // If no text selected, keep the element from mousedown
        return; 
    }

    // If text IS selected, override lastSelectedElement with text details
    let range = selection.getRangeAt(0);
    let container = range.commonAncestorContainer;
    if (container.nodeType === Node.TEXT_NODE) container = container.parentNode;

    if (container.closest('iframe')) return;
    if (!isTextual(container)) return; 

    // Determine the selector for the text's container
    let selector;
    if (window.selectorMode === 'xpath') {
        selector = pinpointMode ? getXPathPinpointSelector(container) : getXPathElasticSelector(container);
    } else { 
        selector = pinpointMode ? getPinpointSelector(container) : getElasticSelector(container);
    }

    if (!selector) return;

    // Override the lastSelectedElement
    lastSelectedElement = { selector, selectedText };

    // Remove the temporary visual click feedback
    document.querySelectorAll('.__scraper_temp_highlight').forEach(el => {
        el.classList.remove('__scraper_temp_highlight');
        el.style.boxShadow = '';
    });

}, true); // Use capturing phase
}

if (!document.getElementById("__scraper_panel")) {
    createTopBar();
    createSidePanel();

}

// Guarantee panel exists on every load and attempt restore + highlight after load
window.addEventListener('load', () => {
  // ensure panel exists
  if (!document.getElementById("__scraper_panel")) {
    createSidePanel();
  }
  // restore and highlight after slight delay to allow page content (images, SPA updates) to settle
  setTimeout(() => {
    restorePanelState();
    // rebuild highlight map from categorySelections (categorySelections may already be set by restore)
    const selectorsToHighlight = {};
    for (const [cat, arr] of Object.entries(categorySelections || {})) {
      selectorsToHighlight[cat] = Array.isArray(arr) ? getCommonSelector(arr) : arr;
    }
    if (Object.keys(selectorsToHighlight).length) {
      highlightAllSelectors(selectorsToHighlight);
    }
  }, 250);
});
"""

bootstrap_js = """
(function(){
    if (window.__my_inject_bootstrap_installed) return;
    window.__my_inject_bootstrap_installed = true;

    // Helper to run an arbitrary script string safely when DOM is ready
    window.__runInjectedScript = function(scriptStr){
        function run() {
            try {
                (new Function(scriptStr))();
                console.log('__runInjectedScript executed');
            } catch (e) {
                console.error('__runInjectedScript error', e);
            }
        }

        if (document.readyState === 'complete' || document.readyState === 'interactive') {
            run();
        } else {
            document.addEventListener('DOMContentLoaded', run, { once: true });
        }
    };

    // Hook pushState/replaceState for SPA navigation
    (function(history){
        var push = history.pushState;
        var replace = history.replaceState;
        history.pushState = function(){
            var ret = push.apply(history, arguments);
            window.dispatchEvent(new CustomEvent('injected:locationchange'));
            return ret;
        };
        history.replaceState = function(){
            var ret = replace.apply(history, arguments);
            window.dispatchEvent(new CustomEvent('injected:locationchange'));
            return ret;
        };
        window.addEventListener('popstate', function(){
            window.dispatchEvent(new CustomEvent('injected:locationchange'));
        });
    })(window.history);

    // Optional: MutationObserver for DOM changes
    window.__my_dom_observer = new MutationObserver(function(mutations){
        window.dispatchEvent(new CustomEvent('injected:dommutation'));
    });
    window.__my_dom_observer.observe(document.documentElement || document.body, { childList: true, subtree: true });

    // -------------------------------
    // Prevent _blank links from opening externally
    document.addEventListener('click', function(e){
        let el = e.target;
        while(el && el.tagName !== 'A') el = el.parentElement;
        if(el && el.tagName === 'A' && el.target === '_blank'){
            e.preventDefault();
            window.location.href = el.href;
            console.log('Redirected _blank link internally:', el.href);
        }
    }, true);

    // Override window.open
    window.open = function(url){
        if(url){
            window.location.href = url;
            console.log('Redirected window.open to current webview:', url);
        }
        return null;
    };
    // -------------------------------

    console.log('bootstrap injected and blank link/window.open handler installed');
})();

"""


def inject_bootstrap_and_run(window, script_to_run):
    try:
        # inject bootstrap first
        window.evaluate_js(bootstrap_js)

        # safely encode the JS string
        safe_payload = json.dumps(script_to_run)
        run_cmd = f"window.__runInjectedScript({safe_payload});"
        window.evaluate_js(run_cmd)
    except Exception as e:
        print("inject_bootstrap_and_run error:", e)


def wait_for_ready(window, timeout=5.0, poll_interval=0.1):
    """Wait for document.readyState to be interactive/complete; return True if ready, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = window.evaluate_js("document.readyState")
            # depending on binding, evaluate_js can return None or a string
            if state and 'complete' in state or state == 'interactive':
                return True
        except Exception:
            # Might fail if page is currently navigating or cross-origin; just retry
            pass
        time.sleep(poll_interval)
    return False


def monitor_url(window, api, interval=0.5):
    last_url = None
    try:
        last_url = window.get_current_url()
        api.track_url(last_url)
        # On initial load, inject bootstrap
        # we do not wait here; webview.start on_loaded should already do injection too.
        inject_bootstrap_and_run(window, js_code)
    except Exception as e:
        print('monitor initial error', e)

    while True:
        time.sleep(interval)
        try:
            current_url = window.get_current_url()
            if current_url != last_url:
                last_url = current_url
                api.track_url(current_url)
                # Wait for document readiness (bounded)
                if wait_for_ready(window, timeout=6.0):
                    # Run bootstrap + script. The bootstrap code itself will ensure DOM-ready inside page.
                    inject_bootstrap_and_run(window, js_code)
                else:
                    # Fallback: try a few retries (in case of slow loading or SPA)
                    for attempt in range(5):
                        try:
                            window.evaluate_js(bootstrap_js)
                            time.sleep(0.25)
                            inject_bootstrap_and_run(window, js_code)
                            break
                        except Exception as e:
                            time.sleep(0.25)
                    else:
                        print('Failed to inject after URL change', current_url)
        except Exception as e:
            # This can happen if the window is closed
            print("Monitor thread exiting.")
            break


# Example usage
def run_template_creator():
    # 1. Run the Flet app first to get the URL
    url = "https://google.pl"
    time.sleep(0.5)

    # 2. If Flet returns a URL, proceed to start pywebview
    if not url:
        print("No URL entered. Exiting.")
        exit()

    # Create window
    window = webview.create_window(
        "Web Scraper GUI Selector",
        url,
        width=1200,
        height=800,
        text_select=True,
        on_top=True
    )

    # Create Api and attach (your Api should accept window later if needed)
    api = Api(window)  # or Api(None); set api.window = window if Api stores window

    # Expose API: depending on your webview version, you might pass js_api when creating the window.
    # Many pywebview versions allow: webview.create_window(..., js_api=api)
    # If your binding supports window.expose(fn) you can do that; check docs.
    for attr in dir(api):
        fn = getattr(api, attr)
        if callable(fn) and not attr.startswith("_"):
            try:
                window.expose(fn)
            except Exception:
                pass

    threading.Thread(target=monitor_url, args=(window, api), daemon=True).start()

    # on_loaded will run once when the initial page has loaded
    def on_loaded(window):

        # install bootstrap and run initial script; inside page it will wait for DOM ready
        inject_bootstrap_and_run(window, js_code)

    webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
    webview.start(on_loaded, window, debug=True)


if __name__ == "__main__":
    run_template_creator()