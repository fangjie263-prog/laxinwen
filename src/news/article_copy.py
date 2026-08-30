"""Shared browser-side logic for copying one article's original text."""

COPY_ARTICLE_JS = r"""
(function () {
  'use strict';
  function textOf(node) {
    return (node && (node.innerText || node.textContent) || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n[ \t]+/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }
  function articleText(button) {
    var article = button.closest('section.article, .page, article');
    if (!article) return '';
    var title = article.querySelector('.article-title, .page-title, h1, h2');
    var original = article.querySelector(
      '[data-copy-role="original"], [data-variant="original"], '
      + '[data-language="original"], .original-body, .original-content, .original'
    );
    var translation = article.querySelector(
      '[data-copy-role="translation"], [data-variant="translation"], '
      + '[data-language="translation"], .translation'
    );
    if (!original && translation) return '';
    var copyRoot = (original || article).cloneNode(true);
    copyRoot.querySelectorAll(
      'button, .copy-article, .read-toggle, .star-toggle, nav, .back-link, '
      + 'a[href="#toc"], img, script, style'
    ).forEach(function (node) { node.remove(); });
    return [textOf(title), textOf(copyRoot)].filter(Boolean).join('\n\n');
  }
  function fallbackCopy(value) {
    var area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    area.remove();
    return ok;
  }
  function copied(button) {
    var old = button.textContent;
    button.textContent = '✓ 已复制';
    window.setTimeout(function () { button.textContent = old || '复制'; }, 1500);
  }
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('.copy-article');
    if (!button) return;
    event.preventDefault();
    var value = articleText(button);
    if (!value) return;
    var result = window.navigator.clipboard && window.navigator.clipboard.writeText
      ? window.navigator.clipboard.writeText(value).then(function () { return true; })
      : Promise.resolve(fallbackCopy(value));
    result.then(function (ok) {
      if (ok !== false) copied(button);
      else if (fallbackCopy(value)) copied(button);
    }).catch(function () {
      if (fallbackCopy(value)) copied(button);
    });
  });
}());
"""
