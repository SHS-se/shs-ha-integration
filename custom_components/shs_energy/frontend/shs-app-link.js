class ShsAppLink extends HTMLElement {
  set hass(value) {
    if (this.opening) return;
    this.opening = true;
    this.textContent = 'Opening SHS Energy settings…';
    value.callWS({type:'shs_energy/app_link'}).then(({url}) => {
      const destination = `${url}/settings${window.location.search}`;
      window.history.replaceState(null,'',destination);
      window.dispatchEvent(new CustomEvent('location-changed', {detail:{replace:true}}));
    }).catch(error => {
      this.textContent = error.message || 'Open SHS Energy from Home Assistant’s Apps page.';
    });
  }
}
if (!customElements.get('shs-app-link')) customElements.define('shs-app-link',ShsAppLink);
