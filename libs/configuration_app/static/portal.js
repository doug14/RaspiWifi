for (const button of document.querySelectorAll('[data-password]')) {
  const input = document.getElementById(button.dataset.password);
  button.hidden = false;
  button.addEventListener('click', () => {
    const visible = input.type === 'password';
    input.type = visible ? 'text' : 'password';
    button.textContent = visible ? 'Ocultar senha' : 'Mostrar senha';
  });
}
