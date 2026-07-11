// login.js — AI-Relay Dashboard Login
// Keine onclick-Handler im HTML — alles via Event Delegation

function showTab(mode) {
  document.getElementById('formUser').classList.toggle('hidden', mode !== 'user');
  document.getElementById('formSeed').classList.toggle('hidden', mode !== 'seed');
  document.getElementById('tabUser').classList.toggle('active', mode === 'user');
  document.getElementById('tabSeed').classList.toggle('active', mode === 'seed');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab').forEach(el => {
    el.addEventListener('click', () => showTab(el.dataset.tab));
  });

  const params = new URLSearchParams(location.search);
  if (params.get('error')) {
    document.getElementById('error').textContent = decodeURIComponent(params.get('error'));
  }
});