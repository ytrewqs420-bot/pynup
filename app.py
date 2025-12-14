import os
import json
import math
import tempfile
import uuid
import base64
import fitz  # PyMuPDF
from flask import Flask, request, send_file, render_template_string, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB limit

# Store temp paths in a global dict for this session (simple local storage)
FILE_STORAGE = {} 

# --- BACKEND PROCESSING LOGIC ---
def process_pdf_logic(file_orders, settings, output_path):
    n_up = int(settings.get('n_up', 1))
    invert = settings.get('invert', False)
    border = settings.get('border', False)
    low_dpi = settings.get('low_dpi', False)
    dpi = 100 if low_dpi else 150
    orient = settings.get('orientation', 'auto')
    
    use_vector = not (invert or low_dpi)

    input_docs = []
    page_map = [] 

    try:
        # 1. Load Documents & Map Pages
        for item in file_orders:
            fid = item['id']
            if fid not in FILE_STORAGE:
                continue
                
            f_path = FILE_STORAGE[fid]
            doc = fitz.open(f_path)
            input_docs.append(doc)
            
            selection = item.get('selected_pages', 'all')
            if selection == 'all':
                indices = range(len(doc))
            else:
                indices = [int(x) for x in selection]
            
            for p in indices:
                if 0 <= p < len(doc):
                    page_map.append((len(input_docs)-1, p))

        if not page_map:
            raise Exception("No pages selected in the queue.")

        # 2. Setup Output Document
        out_doc = fitz.open()
        A4_SHORT, A4_LONG = 595, 842 
        
        # Orientation & Grid Logic
        pw, ph = A4_SHORT, A4_LONG # Default Portrait
        cols, rows = 1, 1

        if n_up == 1:
            if orient == 'auto':
                if page_map:
                    ref = input_docs[page_map[0][0]][page_map[0][1]]
                    pw, ph = ref.rect.width, ref.rect.height
            elif orient == 'landscape': pw, ph = A4_LONG, A4_SHORT
            else: pw, ph = A4_SHORT, A4_LONG
            
        elif n_up == 2:
            is_land = (orient == 'landscape' or orient == 'auto')
            pw, ph = (A4_LONG, A4_SHORT) if is_land else (A4_SHORT, A4_LONG)
            cols, rows = (2, 1) if is_land else (1, 2)
            
        elif n_up == 4:
            is_land = (orient == 'landscape')
            pw, ph = (A4_LONG, A4_SHORT) if is_land else (A4_SHORT, A4_LONG)
            cols, rows = 2, 2
            
        elif n_up == 6:
            # 2x3 for 6-Up Landscape
            is_land = (orient == 'landscape' or orient == 'auto')
            if is_land:
                pw, ph = A4_LONG, A4_SHORT
                cols, rows = 2, 3 
            else:
                pw, ph = A4_SHORT, A4_LONG
                cols, rows = 2, 3

        cell_w, cell_h = pw / cols, ph / rows
        pages_per_sheet = n_up
        num_sheets = math.ceil(len(page_map) / pages_per_sheet)

        # 3. Render Pages
        for sheet_idx in range(num_sheets):
            out_page = out_doc.new_page(width=pw, height=ph)
            
            for i in range(pages_per_sheet):
                global_idx = (sheet_idx * pages_per_sheet) + i
                if global_idx >= len(page_map): break
                
                doc_idx, p_num = page_map[global_idx]
                src_doc = input_docs[doc_idx]
                src_page = src_doc[p_num]
                
                # Calculate Grid Position
                c = i % cols
                r = i // cols
                x = c * cell_w
                y = r * cell_h
                rect = fitz.Rect(x, y, x+cell_w, y+cell_h)
                
                if use_vector:
                    out_page.show_pdf_page(rect, src_doc, p_num)
                else:
                    # Raster path
                    mat = fitz.Matrix(dpi/72, dpi/72)
                    pix = src_page.get_pixmap(matrix=mat, alpha=False)
                    if invert: pix.invert_irect(pix.irect)
                    img_data = pix.tobytes("jpeg", jpg_quality=85)
                    out_page.insert_image(rect, stream=img_data)
                    pix = None
                
                if border:
                    out_page.draw_rect(rect, color=(0,0,0), width=0.5)

        out_doc.save(output_path)
        out_doc.close()
    finally:
        for doc in input_docs: doc.close()


# --- FLASK ROUTES ---

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
        file = request.files['file']
        if file.filename == '': return jsonify({'error': 'No selected file'}), 400

        file_id = str(uuid.uuid4())
        ext = os.path.splitext(file.filename)[1]
        temp_path = os.path.join(tempfile.gettempdir(), f"pynup_{file_id}{ext}")
        file.save(temp_path)
        FILE_STORAGE[file_id] = temp_path
        
        with fitz.open(temp_path) as doc: count = len(doc)
        return jsonify({'id': file_id, 'pages': count, 'message': 'Upload successful'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/thumbnails/<file_id>', methods=['GET'])
def get_thumbnails(file_id):
    if file_id not in FILE_STORAGE: return jsonify({'error': 'File not found'}), 404
    try:
        path = FILE_STORAGE[file_id]
        doc = fitz.open(path)
        thumbnails = []
        limit = min(len(doc), 100) 
        for i in range(limit):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2))
            data = pix.tobytes("png")
            b64_str = base64.b64encode(data).decode('utf-8')
            thumbnails.append(f"data:image/png;base64,{b64_str}")
        return jsonify({'thumbnails': thumbnails, 'total': len(doc)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/process', methods=['POST'])
def process():
    try:
        data = request.json
        file_orders = data.get('files', [])
        settings = data.get('settings', {})
        if not file_orders: return jsonify({'error': 'No files in queue'}), 400

        temp_dir = tempfile.gettempdir()
        output_filename = f"pynup_processed_{uuid.uuid4().hex[:6]}.pdf"
        output_path = os.path.join(temp_dir, output_filename)
        
        process_pdf_logic(file_orders, settings, output_path)
        return send_file(output_path, as_attachment=True, download_name="processed_document.pdf")
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- FRONTEND TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyNUp Web</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
        }
    </script>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>

    <style>
        body { font-family: 'Segoe UI', sans-serif; transition: background-color 0.3s, color 0.3s; }
        .drag-active { border-color: #3b82f6; background-color: rgba(59, 130, 246, 0.1); }
        .scrollbar-hide::-webkit-scrollbar { display: none; }
    </style>
</head>
<body class="bg-gray-100 text-slate-800 dark:bg-slate-950 dark:text-slate-200">
    <div id="root"></div>

    <script type="text/babel">
        const { useState, useEffect } = React;

        // ICONS
        const IconUpload = () => <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>;
        const IconGrid = () => <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>;
        const IconTrash = () => <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>;
        const IconCheck = () => <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><polyline points="20 6 9 17 4 12"/></svg>;
        const IconX = () => <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>;
        const IconSun = () => <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>;
        const IconMoon = () => <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>;

        function PageSelectorModal({ isOpen, file, onClose, onSave }) {
            const [thumbnails, setThumbnails] = useState([]);
            const [selected, setSelected] = useState(new Set());
            const [loading, setLoading] = useState(true);

            useEffect(() => {
                if (isOpen && file) {
                    setLoading(true);
                    if (file.selectedPages === 'all') {
                        setSelected(new Set(Array.from({length: file.pages}, (_, i) => i)));
                    } else {
                        setSelected(new Set(file.selectedPages));
                    }
                    fetch(`/thumbnails/${file.id}`)
                        .then(r => r.json())
                        .then(data => {
                            if(data.thumbnails) setThumbnails(data.thumbnails);
                            setLoading(false);
                        })
                        .catch(err => { console.error(err); setLoading(false); });
                }
            }, [isOpen, file]);

            if (!isOpen) return null;

            const togglePage = (idx) => {
                const next = new Set(selected);
                if (next.has(idx)) next.delete(idx); else next.add(idx);
                setSelected(next);
            };

            const handleSave = () => {
                if (selected.size === file.pages) onSave('all');
                else onSave(Array.from(selected).sort((a,b) => a-b));
                onClose();
            };

            return (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
                    <div className="bg-white dark:bg-slate-900 w-full max-w-5xl h-[85vh] rounded-2xl border border-gray-200 dark:border-slate-700 flex flex-col shadow-2xl overflow-hidden">
                        <div className="p-4 border-b border-gray-200 dark:border-slate-700 flex justify-between items-center bg-gray-50 dark:bg-slate-800/50">
                            <div><h3 className="text-lg font-bold text-gray-800 dark:text-white flex items-center gap-2"><IconGrid /> Select Pages: {file.name}</h3><p className="text-sm text-gray-500 dark:text-slate-400">{selected.size} / {file.pages} pages selected</p></div>
                            <button onClick={onClose} className="text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-white"><IconX /></button>
                        </div>
                        <div className="p-3 bg-gray-50 dark:bg-slate-950 border-b border-gray-200 dark:border-slate-800 flex gap-2">
                            <button onClick={() => setSelected(new Set(Array.from({length: file.pages}, (_, i) => i)))} className="px-3 py-1 text-xs font-medium bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 hover:bg-gray-100 dark:hover:bg-slate-700 text-gray-700 dark:text-white rounded">Select All</button>
                            <button onClick={() => setSelected(new Set())} className="px-3 py-1 text-xs font-medium bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 hover:bg-gray-100 dark:hover:bg-slate-700 text-gray-700 dark:text-white rounded">Deselect All</button>
                            <button onClick={() => { const next = new Set(); for(let i=0; i<file.pages; i++) if(!selected.has(i)) next.add(i); setSelected(next); }} className="px-3 py-1 text-xs font-medium bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 hover:bg-gray-100 dark:hover:bg-slate-700 text-gray-700 dark:text-white rounded">Invert</button>
                        </div>
                        <div className="flex-1 overflow-y-auto p-6 bg-gray-100 dark:bg-slate-950">
                            {loading ? <div className="text-gray-500 dark:text-slate-500 text-center">Loading thumbnails...</div> : (
                                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
                                    {thumbnails.map((src, idx) => {
                                        const isSel = selected.has(idx);
                                        return (
                                            <div key={idx} onClick={() => togglePage(idx)} className={`relative cursor-pointer group rounded-lg overflow-hidden border-2 transition-all ${isSel ? 'border-blue-500 shadow-lg shadow-blue-500/20' : 'border-gray-200 dark:border-slate-800 opacity-60 hover:opacity-100 hover:border-gray-400 dark:hover:border-slate-600'}`}>
                                                <div className="aspect-[1/1.4] bg-white dark:bg-slate-900"><img src={src} className="w-full h-full object-contain" /></div>
                                                <div className={`absolute top-2 right-2 w-6 h-6 rounded-full flex items-center justify-center text-white text-xs font-bold transition ${isSel ? 'bg-blue-500' : 'bg-black/50'}`}>{isSel ? <IconCheck /> : idx+1}</div>
                                            </div>
                                        )
                                    })}
                                </div>
                            )}
                        </div>
                        <div className="p-4 border-t border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex justify-end gap-3">
                            <button onClick={onClose} className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-slate-400 hover:text-gray-900 dark:hover:text-white">Cancel</button>
                            <button onClick={handleSave} className="px-6 py-2 text-sm font-bold bg-blue-600 hover:bg-blue-500 text-white rounded-lg shadow-lg">Save Selection</button>
                        </div>
                    </div>
                </div>
            );
        }

        function App() {
            const [files, setFiles] = useState([]); 
            const [settings, setSettings] = useState({ n_up: "1", orientation: "auto", invert: false, low_dpi: false, border: false });
            const [isProcessing, setIsProcessing] = useState(false);
            const [modal, setModal] = useState({ open: false, fileId: null });
            const [theme, setTheme] = useState('dark');

            useEffect(() => {
                if (theme === 'dark') document.documentElement.classList.add('dark');
                else document.documentElement.classList.remove('dark');
            }, [theme]);

            const toggleTheme = () => setTheme(prev => prev === 'dark' ? 'light' : 'dark');

            const handleFiles = async (fileList) => {
                const newUploads = Array.from(fileList).map(f => ({ tempId: Math.random(), fileObj: f, name: f.name, pages: 0, status: 'uploading', selectedPages: 'all' }));
                setFiles(prev => [...prev, ...newUploads]);
                for (let item of newUploads) {
                    const fd = new FormData(); fd.append('file', item.fileObj);
                    try {
                        const res = await fetch('/upload', { method: 'POST', body: fd });
                        const data = await res.json();
                        setFiles(prev => prev.map(f => f.tempId === item.tempId ? { ...f, id: data.id, pages: data.pages, status: 'done' } : f));
                    } catch (err) {
                        alert(`Failed to upload ${item.name}`);
                        setFiles(prev => prev.filter(f => f.tempId !== item.tempId));
                    }
                }
            };

            const openSelector = (id) => { const f = files.find(f => f.id === id); if (f && f.status === 'done') setModal({ open: true, fileId: id }); };
            const saveSelection = (fileId, newSelection) => { setFiles(prev => prev.map(f => f.id === fileId ? { ...f, selectedPages: newSelection } : f)); };

            const handleSubmit = async () => {
                if (files.some(f => f.status === 'uploading')) return alert("Please wait for uploads to finish");
                if (files.length === 0) return alert("Queue is empty");
                setIsProcessing(true);
                try {
                    const payload = { files: files.map(f => ({ id: f.id, selected_pages: f.selectedPages })), settings };
                    const res = await fetch('/process', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    if (!res.ok) throw new Error("Processing failed");
                    
                    const blob = await res.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    
                    const firstFile = files[0].name.replace('.pdf', '');
                    const nUpSuffix = settings.n_up > 1 ? `_${settings.n_up}up` : '';
                    const effectSuffix = settings.invert ? '_inverted' : '';
                    const countSuffix = files.length > 1 ? '_merged' : '';
                    a.download = `${firstFile}${countSuffix}${nUpSuffix}${effectSuffix}.pdf`;
                    
                    document.body.appendChild(a); a.click(); document.body.removeChild(a);
                } catch (err) { alert(err.message); } finally { setIsProcessing(false); }
            };

            const activeFile = modal.fileId ? files.find(f => f.id === modal.fileId) : null;

            return (
                <div className="min-h-screen p-6 md:p-12 max-w-6xl mx-auto transition-colors duration-300">
                    {activeFile && <PageSelectorModal isOpen={modal.open} file={activeFile} onClose={() => setModal({ open: false, fileId: null })} onSave={(sel) => saveSelection(modal.fileId, sel)} />}
                    
                    <header className="mb-10 flex items-center justify-between">
                        <div>
                            <h1 className="text-4xl font-extrabold bg-gradient-to-r from-blue-500 to-indigo-500 bg-clip-text text-transparent mb-2">PyNUp Web</h1>
                            <p className="text-gray-500 dark:text-slate-400">Visual PDF Layout Tool</p>
                        </div>
                        <button onClick={toggleTheme} className="p-3 rounded-full bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 text-gray-500 dark:text-slate-400 hover:text-blue-500 dark:hover:text-blue-400 transition shadow-sm">
                            {theme === 'dark' ? <IconSun /> : <IconMoon />}
                        </button>
                    </header>

                    <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                        <div className="lg:col-span-2 space-y-6">
                            <div className="border-2 border-dashed border-gray-300 dark:border-slate-700 bg-white dark:bg-slate-800/30 rounded-xl p-8 text-center hover:bg-gray-50 dark:hover:bg-slate-800/50 transition relative">
                                <input type="file" multiple accept="application/pdf" className="absolute inset-0 opacity-0 cursor-pointer" onChange={(e) => handleFiles(e.target.files)} />
                                <div className="flex flex-col items-center gap-4"><div className="w-16 h-16 bg-blue-500/10 rounded-full flex items-center justify-center text-blue-500 dark:text-blue-400"><IconUpload /></div><div><p className="text-lg font-medium text-gray-700 dark:text-slate-200">Drag & Drop or Click to Upload</p></div></div>
                            </div>
                            <div className="bg-white dark:bg-slate-900 rounded-xl border border-gray-200 dark:border-slate-800 overflow-hidden min-h-[300px] shadow-sm">
                                {files.length === 0 ? <div className="p-8 text-center text-gray-400 dark:text-slate-500 italic mt-10">Drop PDFs here to begin</div> : 
                                    <div className="divide-y divide-gray-100 dark:divide-slate-800">{files.map((file, idx) => (
                                        <div key={file.tempId || file.id} className="p-4 flex items-center gap-4 hover:bg-gray-50 dark:hover:bg-slate-800/50 transition group">
                                            <div className="w-8 text-center text-gray-400 dark:text-slate-600 font-mono text-xs">{idx+1}</div>
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2"><p className="font-medium text-gray-800 dark:text-slate-200 truncate">{file.name}</p>{file.status === 'uploading' && <span className="text-xs text-blue-500 dark:text-blue-400 animate-pulse">Uploading...</span>}</div>
                                                {file.status === 'done' && <p className="text-xs text-gray-500 dark:text-slate-500">{file.selectedPages === 'all' ? `All ${file.pages} pages included` : `${file.selectedPages.length} of ${file.pages} pages selected`}</p>}
                                            </div>
                                            {file.status === 'done' && <button onClick={() => openSelector(file.id)} className="px-3 py-1.5 text-xs font-bold bg-gray-100 dark:bg-slate-800 hover:bg-blue-600 text-blue-600 dark:text-blue-400 hover:text-white rounded border border-gray-200 dark:border-slate-700 transition flex items-center gap-2"><IconGrid /> Select Pages</button>}
                                            <button onClick={() => setFiles(f => f.filter((_, i) => i !== idx))} className="p-2 text-gray-400 dark:text-slate-500 hover:text-red-500 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 transition"><IconTrash /></button>
                                        </div>
                                    ))}</div>
                                }
                            </div>
                        </div>

                        <div className="bg-white dark:bg-slate-900 rounded-xl border border-gray-200 dark:border-slate-800 p-6 h-fit space-y-8 shadow-sm">
                            <div>
                                <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">Settings</h2>
                                <div className="space-y-6">
                                    <div><label className="text-xs font-bold text-gray-500 dark:text-slate-500 uppercase tracking-wider mb-2 block">Layout</label><div className="grid grid-cols-4 gap-2">{['1', '2', '4', '6'].map(n => (<button key={n} onClick={() => setSettings({...settings, n_up: n})} className={`py-2 rounded font-bold text-sm transition ${settings.n_up == n ? 'bg-blue-600 text-white shadow-lg shadow-blue-500/25' : 'bg-gray-100 dark:bg-slate-950 text-gray-500 dark:text-slate-400 hover:bg-gray-200 dark:hover:bg-slate-800'}`}>{n}-Up</button>))}</div></div>
                                    <div><label className="text-xs font-bold text-gray-500 dark:text-slate-500 uppercase tracking-wider mb-2 block">Orientation</label><select value={settings.orientation} onChange={(e) => setSettings({...settings, orientation: e.target.value})} className="w-full bg-gray-50 dark:bg-slate-950 border border-gray-200 dark:border-slate-700 rounded p-2.5 text-gray-800 dark:text-slate-200 text-sm outline-none focus:border-blue-500"><option value="auto">Auto (Detect)</option><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></div>
                                    <div className="space-y-3">
                                        <label className="flex items-center gap-3 cursor-pointer group select-none"><div className={`w-5 h-5 rounded border flex items-center justify-center transition ${settings.invert ? 'bg-purple-600 border-purple-500' : 'border-gray-300 dark:border-slate-600 bg-gray-50 dark:bg-slate-950'}`}>{settings.invert && <IconCheck className="text-white"/>}</div><input type="checkbox" className="hidden" checked={settings.invert} onChange={() => setSettings({...settings, invert: !settings.invert})} /><span className="text-sm text-gray-600 dark:text-slate-300">Invert Colors</span></label>
                                        <label className="flex items-center gap-3 cursor-pointer group select-none"><div className={`w-5 h-5 rounded border flex items-center justify-center transition ${settings.low_dpi ? 'bg-amber-600 border-amber-500' : 'border-gray-300 dark:border-slate-600 bg-gray-50 dark:bg-slate-950'}`}>{settings.low_dpi && <IconCheck className="text-white"/>}</div><input type="checkbox" className="hidden" checked={settings.low_dpi} onChange={() => setSettings({...settings, low_dpi: !settings.low_dpi})} /><span className="text-sm text-gray-600 dark:text-slate-300">Low DPI (Save RAM)</span></label>
                                        <label className="flex items-center gap-3 cursor-pointer group select-none"><div className={`w-5 h-5 rounded border flex items-center justify-center transition ${settings.border ? 'bg-emerald-600 border-emerald-500' : 'border-gray-300 dark:border-slate-600 bg-gray-50 dark:bg-slate-950'}`}>{settings.border && <IconCheck className="text-white"/>}</div><input type="checkbox" className="hidden" checked={settings.border} onChange={() => setSettings({...settings, border: !settings.border})} /><span className="text-sm text-gray-600 dark:text-slate-300">Add Borders</span></label>
                                    </div>
                                </div>
                            </div>
                            <button onClick={handleSubmit} disabled={isProcessing} className="w-full py-4 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white font-bold rounded-xl shadow-lg shadow-blue-500/25 transition disabled:opacity-50 disabled:cursor-not-allowed">{isProcessing ? 'Processing...' : 'Merge & Download PDF'}</button>
                        </div>
                    </div>
                </div>
            );
        }

        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(<App />);
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    print("Starting Flask Server...")
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host='0.0.0.0', debug=True, port=5000)