const form = document.querySelector('#search');
const tenant = document.querySelector('#tenant');
const query = document.querySelector('#query');
const results = document.querySelector('#results');
const status = document.querySelector('#status');
let generation = 0;
async function get(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
async function search(event) {
  if (event) event.preventDefault();
  const request = ++generation;
  const workspace = tenant.value;
  status.textContent = 'Searching…';
  results.replaceChildren();
  document.querySelector('#source-panel').hidden = true;
  try {
    const data = await get('/api/search?' + new URLSearchParams({tenant: workspace, q: query.value}));
    if (request !== generation) return;
    status.textContent = data.results.length ? `${data.results.length} source excerpts` : 'No supporting evidence found. Try a different term.';
    for (const result of data.results) {
      const card = document.createElement('article');
      const citation = document.createElement('button');
      citation.className = 'citation';
      citation.textContent = result.citation;
      citation.addEventListener('click', async () => {
        try {
          const source = await get('/api/document?' + new URLSearchParams({tenant: workspace, id: result.document_id, version: result.version}));
          if (request !== generation) return;
          document.querySelector('#source-title').textContent = `${source.source} · version ${source.version}`;
          document.querySelector('#source-text').textContent = source.content.split('\n').map((line, i) => `${String(i + 1).padStart(3)}  ${line}`).join('\n');
          document.querySelector('#source-panel').hidden = false;
          document.querySelector('#source-panel').scrollIntoView({behavior: 'smooth'});
        } catch (error) { if (request === generation) status.textContent = error.message; }
      });
      const excerpt = document.createElement('pre');
      excerpt.textContent = result.content; // Untrusted source content is never HTML.
      card.append(citation, excerpt);
      results.append(card);
    }
  } catch (error) { if (request === generation) status.textContent = error.message; }
}
form.addEventListener('submit', search);
tenant.addEventListener('change', () => { query.value = tenant.value === 'investment' ? 'grid connection' : 'housing repairs'; search(); });
document.querySelectorAll('[data-query]').forEach(button => button.addEventListener('click', () => { tenant.value = button.dataset.tenant; query.value = button.dataset.query; search(); }));
search();
