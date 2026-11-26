(function () {
  var browser = document.querySelector('.inv-browser');
  if (!browser) return;

  var detail = document.getElementById('inv-detail');
  var form = document.getElementById('inv-detail-form');
  if (!detail || !form) return;

  var tiles = Array.prototype.slice.call(browser.querySelectorAll('.inv-tile'));
  var imageWrap = document.getElementById('inv-detail-img-wrap');
  var image = document.getElementById('inv-detail-image');
  var imagePh = document.getElementById('inv-detail-image-ph');
  var nameEl = document.getElementById('inv-detail-name');
  var typeEl = document.getElementById('inv-detail-type');
  var rarityEl = document.getElementById('inv-detail-rarity');
  var raritySelect = document.getElementById('inv-detail-rarity-select');
  var stateEl = document.getElementById('inv-detail-state');
  var appidInput = form.querySelector('input[name="appid"]');
  var assetInput = form.querySelector('input[name="assetid"]');
  var priceInput = document.getElementById('inv-detail-price');

  function initials(name) {
    var letters = String(name || '').replace(/\|/g, ' ').split(/\s+/).filter(function (word) {
      return word && /^[a-zа-яё0-9]/i.test(word);
    });
    return letters.slice(0, 2).map(function (word) {
      return word.charAt(0).toUpperCase();
    }).join('') || '?';
  }

  function stateText(state) {
    if (state === 'imported') return 'Этот предмет уже добавлен в SKINMARKET и выставлен на продажу.';
    if (state === 'blocked') return 'Этот предмет нельзя передать, поэтому добавить его в каталог нельзя.';
    return 'Предмет можно добавить в каталог. Укажите цену — объявление сразу появится на витрине.';
  }

  function selectTile(tile) {
    var state = tile.dataset.state || 'available';
    var name = tile.dataset.name || 'Предмет Steam';
    var imageUrl = tile.dataset.image || '';

    tiles.forEach(function (item) {
      item.classList.toggle('is-selected', item === tile);
    });

    if (imageWrap) imageWrap.className = 'inv-detail-img rarity-' + (tile.dataset.rarityCls || 'common');
    if (nameEl) nameEl.textContent = name;
    if (typeEl) typeEl.textContent = tile.dataset.type || 'Предмет Steam';
    if (rarityEl) {
      rarityEl.textContent = tile.dataset.rarity || 'Обычное';
      rarityEl.className = 'tag tag-' + (tile.dataset.rarityCls || 'common');
    }
    if (raritySelect && tile.dataset.rarityRu) raritySelect.value = tile.dataset.rarityRu;
    if (stateEl) stateEl.textContent = stateText(state);

    if (image && imagePh) {
      if (imageUrl) {
        image.src = imageUrl;
        image.alt = name;
        image.hidden = false;
        imagePh.hidden = true;
      } else {
        image.hidden = true;
        image.removeAttribute('src');
        imagePh.textContent = initials(name);
        imagePh.hidden = false;
      }
    }

    if (appidInput) appidInput.value = tile.dataset.appid || '';
    if (assetInput) assetInput.value = tile.dataset.assetid || '';
    form.hidden = state !== 'available';
    if (priceInput) priceInput.value = '';

    if (window.history && window.history.replaceState) {
      try {
        window.history.replaceState(null, '', tile.getAttribute('href'));
      } catch (error) {
        // В некоторых браузерах файловый URL нельзя заменить — просто игнорируем.
      }
    }
  }

  tiles.forEach(function (tile) {
    tile.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      selectTile(tile);
    });
  });
})();
