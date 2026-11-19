// Step details: show the step chosen in the picker. Without JavaScript every step stays visible.
(function () {
  var pick = document.getElementById('step-pick');
  if (!pick) return;
  var steps = Array.prototype.slice.call(document.querySelectorAll('.step'));
  function show() {
    steps.forEach(function (s) { s.hidden = s.id !== pick.value; });
  }
  var h = location.hash.slice(1);
  if (h && document.getElementById(h) && document.getElementById(h).classList.contains('step')) pick.value = h;
  pick.addEventListener('change', function () {
    show();
    try { history.replaceState(null, '', '#' + pick.value); } catch (e) {}
  });
  show();
})();
