(function () {
  // Показывает в списке категорий только те, что относятся к выбранной игре.
  // opts.allLabel: если игра не выбрана, показываются все категории (для фильтров), иначе список пуст.
  window.bindCategoryFilter = function (game, cat, opts) {
    opts = opts || {};
    var options = Array.prototype.slice.call(cat.querySelectorAll('option[data-game]'));
    function filter() {
      var shown = 0;
      options.forEach(function (o) {
        var ok = game.value ? o.getAttribute('data-game') === game.value : !!opts.allLabel;
        o.hidden = !ok;
        o.disabled = !ok;
        if (ok) shown++;
        var name = o.getAttribute('data-name');
        if (name) {
          o.textContent = game.value ? name : name + ' \u00b7 ' + o.getAttribute('data-gamename');
        }
      });
      if (cat.selectedOptions[0] && cat.selectedOptions[0].disabled) cat.value = '';
      if (!opts.allLabel) {
        cat.options[0].textContent = game.value
          ? (shown ? 'Выберите…' : 'В этой игре нет категорий')
          : 'Сначала выберите игру';
      }
    }
    game.addEventListener('change', filter);
    filter();
  };

  // Предпросмотр выбранной картинки перед загрузкой.
  window.bindPreview = function (input, img) {
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) { img.hidden = true; return; }
      img.src = URL.createObjectURL(file);
      img.hidden = false;
    });
  };
})();
