'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => new Intl.NumberFormat('en-IN', {style:'currency',currency:'INR'}).format(Number(value));
const statusText = status => ({PAYMENT_CAPTURED:'Paid',PENDING_PAYMENT:'Awaiting payment',CANCELLED:'Cancelled'}[status] || status);
function readStorage(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
function saveStorage(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { throw new Error('Please enable browser storage to save your cart and order details.'); } }
let products = [], config = {}, cart = readStorage('xyz-cart', {});
if (!cart || typeof cart !== 'object' || Array.isArray(cart)) cart = {};
let savedOrders = readStorage('xyz-orders', []);
if (!Array.isArray(savedOrders)) savedOrders = [];
let attempt = readStorage('xyz-checkout', null);
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json', ...options.headers}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Please check the form details and try again.');
  return body;
}
function message(selector, text, error = true) { const node=$(selector); node.textContent=text; node.hidden=!text; node.classList.toggle('error',error); }
function report(error) {
  const dialog=document.querySelector('dialog[open]');
  if(!dialog) return message('#page-message',error.message);
  let node=dialog.querySelector('.runtime-error');
  if(!node){node=document.createElement('div');node.className='notice error runtime-error';node.setAttribute('role','alert');dialog.append(node);}
  node.textContent=error.message;
}
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
function cartLines() { return products.filter(p => Number.isInteger(cart[p.product_id]) && cart[p.product_id] > 0).map(p => ({...p,quantity:cart[p.product_id]})); }
function subtotal() { return cartLines().reduce((total,p) => total + Math.round(Number(p.price)*100)*p.quantity,0)/100; }
function renderProducts() {
  const search=$('#search').value.toLowerCase(), category=$('#category').value, sort=$('#sort').value;
  const visible=products.filter(p => (!category || p.category===category) && `${p.name} ${p.description} ${p.sku}`.toLowerCase().includes(search));
  visible.sort((a,b) => sort==='price-low' ? Number(a.price)-Number(b.price) : sort==='price-high' ? Number(b.price)-Number(a.price) : a.name.localeCompare(b.name));
  $('#product-count').textContent=`${visible.length} ${visible.length===1?'product':'products'} to explore`;
  $('#products').innerHTML=visible.map(p => `<article class="product"><div class="product-image">${p.image_url ? `<img src="${esc(p.image_url)}" alt="${esc(p.name)}" loading="lazy" referrerpolicy="no-referrer">` : `<span class="placeholder" aria-hidden="true">${esc(p.name.slice(0,1))}</span>`}</div><div class="product-body"><span class="category">${esc(p.category)}</span><h3>${esc(p.name)}</h3><p>${esc(p.description.slice(0,100))}</p><div class="product-bottom"><span class="price">${money(p.price)}</span><button data-add="${p.product_id}" ${p.available<1?'disabled':''}>${p.available<1?'Sold out':'Add to bag +'}</button></div><div class="stock">${p.available>0?`${p.available} available`:'Check back soon'}</div></div></article>`).join('') || '<div class="empty">No products found. Try another search or category.</div>';
  $('#products').querySelectorAll('img').forEach(img => img.addEventListener('error', () => { img.replaceWith(Object.assign(document.createElement('span'),{className:'placeholder',textContent:img.alt.slice(0,1)})); }));
}
function renderCart() {
  const lines=cartLines();
  $('#cart-count').textContent=lines.reduce((sum,p) => sum+p.quantity,0);
  $('#cart-items').innerHTML=lines.map(p => `<div class="cart-line"><div><strong>${esc(p.name)}</strong><small>${money(p.price)} each</small></div><input data-quantity="${p.product_id}" type="number" min="1" max="${p.available}" value="${p.quantity}" aria-label="Quantity for ${esc(p.name)}"><span>${money(Number(p.price)*p.quantity)}</span><button class="plain" data-remove="${p.product_id}" aria-label="Remove ${esc(p.name)}">✕</button></div>`).join('') || '<div class="empty">Your bag is empty. Find something you love in the collection.</div>';
  $('#cart-totals').innerHTML=`<div class="total-row grand"><span>Subtotal</span><span>${money(subtotal())}</span></div><p class="muted">Delivery is calculated at checkout.</p>`;
  $('#begin-checkout').disabled=!lines.length;
}
function persistCart() { saveStorage('xyz-cart',cart); renderCart(); }
$('#products').addEventListener('click', event => {
  const id=event.target.closest('[data-add]')?.dataset.add;
  if (!id) return;
  const product=products.find(p => p.product_id===id);
  if ((cart[id]||0)>=product.available) return report(new Error('All available units of this product are already in your bag.'));
  cart[id]=(cart[id]||0)+1;
  try { persistCart(); message('#page-message',''); $('#cart-dialog').showModal(); } catch(error) { report(error); }
});
$('#cart-items').addEventListener('click', event => { const id=event.target.closest('[data-remove]')?.dataset.remove; if(id){delete cart[id];try{persistCart();}catch(error){report(error);}} });
$('#cart-items').addEventListener('change', event => {
  const id=event.target.dataset.quantity;
  if(!id) return;
  const available=products.find(p=>p.product_id===id).available;
  cart[id]=Math.max(1,Math.min(available,Math.trunc(Number(event.target.value))||1));
  try{persistCart();}catch(error){report(error);}
});
['#search','#category','#sort'].forEach(selector => $(selector).addEventListener('input',renderProducts));
$('#open-cart').addEventListener('click', () => { renderCart();$('#cart-dialog').showModal(); });
function checkoutTotal() {
  const rates=config.shipping_rates || {}, country=$('#country').value;
  const fee=Number(rates[country] ?? rates['*'] ?? 0);
  $('#checkout-total').innerHTML=`<div class="total-row"><span>Products</span><span>${money(subtotal())}</span></div><div class="total-row"><span>Delivery</span><span>${money(fee)}</span></div><div class="total-row grand"><span>Total</span><span>${money(subtotal()+fee)}</span></div>`;
}
$('#country').addEventListener('change',checkoutTotal);
$('#begin-checkout').addEventListener('click', () => {
  $('#cart-dialog').close(); checkoutTotal(); $('#checkout-dialog').showModal();
  message('#checkout-message',config.payments_ready ? (config.payment_test_mode?'Test checkout: no real money will be charged.':'') : 'Online checkout is not available yet. Please contact the store.',false);
  $('#pay-button').disabled=!config.payments_ready;
});
let paymentScript;
function loadPayments() {
  if(window.Razorpay) return Promise.resolve();
  if(!paymentScript) paymentScript=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://checkout.razorpay.com/v1/checkout.js';script.onload=resolve;script.onerror=()=>{paymentScript=null;script.remove();reject(new Error('Could not load secure checkout. Please try again.'));};document.head.append(script);});
  return paymentScript;
}
function rememberOrder(order, token) {
  if(!savedOrders.some(o=>o.order_id===order.order_id)) savedOrders.unshift({order_id:order.order_id,token,gateway:order});
  saveStorage('xyz-orders',savedOrders);
}
function openPayment(order, token, customer) {
  const payment=new window.Razorpay({key:order.key_id,order_id:order.gateway_order_id,amount:order.amount,currency:order.currency,name:config.name,prefill:{name:customer.name,email:customer.email,contact:customer.phone},
    modal:{ondismiss:()=>{message('#page-message','Payment was not completed. Your pending order is saved under My orders.',false);}},
    handler:async result=>{
      try {
        await api(`/api/orders/${order.order_id}/verify`,{method:'POST',headers:{'X-Order-Token':token},body:JSON.stringify(result)});
        cart={};attempt=null;saveStorage('xyz-checkout',null);persistCart();
        await showOrder(order.order_id,token);
        products=await api('/api/products');renderProducts();
      }catch(error){report(new Error(`${error.message} Your order is saved under My orders; refresh its status there.`));}
    }});
  payment.open();
}
$('#checkout-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=$('#pay-button');button.disabled=true;message('#checkout-message','');
  try{
    const customer=Object.fromEntries(new FormData(event.target));
    const items=cartLines().map(p=>({product_id:p.product_id,quantity:p.quantity}));
    if(!items.length) throw new Error('Your bag is empty.');
    const fingerprint=JSON.stringify({items,customer});
    if(!attempt || attempt.fingerprint!==fingerprint){attempt={fingerprint,request_id:crypto.randomUUID(),access_token:crypto.randomUUID().replaceAll('-','')+crypto.randomUUID().replaceAll('-','')};saveStorage('xyz-checkout',attempt);}
    await loadPayments();
    const order=await api('/api/checkout',{method:'POST',body:JSON.stringify({items,customer,request_id:attempt.request_id,access_token:attempt.access_token})});
    rememberOrder(order,attempt.access_token);$('#checkout-dialog').close();openPayment(order,attempt.access_token,customer);
  }catch(error){message('#checkout-message',error.message);if(error.message.includes('checkout has already ended')){attempt=null;saveStorage('xyz-checkout',null);}}
  finally{button.disabled=!config.payments_ready;}
});
$('#open-orders').addEventListener('click',()=>{
  $('#customer-orders').innerHTML=savedOrders.map(order=>`<div class="cart-line" data-order="${esc(order.order_id)}" style="grid-template-columns:1fr auto;cursor:pointer"><strong>Order ${esc(order.order_id.slice(0,8).toUpperCase())}</strong><button data-order="${esc(order.order_id)}" class="secondary">View order</button></div>`).join('') || '<div class="empty">You have not placed an order on this browser yet.</div>';
  $('#orders-dialog').showModal();
});
$('#customer-orders').addEventListener('click',async event=>{
  const id=event.target.closest('[data-order]')?.dataset.order;if(!id)return;
  const order=savedOrders.find(o=>o.order_id===id);$('#orders-dialog').close();
  try{await showOrder(id,order.token);}catch(error){report(error);}
});
async function showOrder(id,token){
  const data=await api(`/api/orders/${id}`,{headers:{'X-Order-Token':token}});
  $('#receipt-content').replaceChildren();
  $('#order-content').innerHTML=`<p class="eyebrow">Order ${esc(id.slice(0,8).toUpperCase())}</p><h2>${esc(statusText(data.status))}</h2><p class="muted">${esc(data.fulfilment.replaceAll('_',' '))}</p><div>${data.items.map(item=>`<div class="total-row" data-product-id="${esc(item.product_id||'')}" style="cursor:pointer;align-items:center;padding:8px 0" title="Click to view item in shop">
    <div style="display:flex;align-items:center;gap:12px">
      ${item.image_url?`<img src="${esc(item.image_url)}" alt="${esc(item.name||item.sku)}" style="width:40px;height:40px;object-fit:cover;border-radius:6px;border:1px solid var(--line)">`:'<span style="width:40px;height:40px;background:#f0f1ea;border-radius:6px;display:grid;place-items:center">📦</span>'}
      <div>
        <strong>${esc(item.name||item.sku)}</strong>
        <small class="muted" style="display:block">${esc(item.sku)} × ${item.quantity} · ${money(item.price)} each</small>
      </div>
    </div>
    <span>${money(Number(item.price)*item.quantity)}</span>
  </div>`).join('')}</div><div class="total-row"><span>Delivery</span><span>${money(data.shipping)}</span></div><div class="total-row grand"><span>Total</span><span>${money(data.total)}</span></div>${data.tracking_url?`<p><a class="button secondary" target="_blank" rel="noopener noreferrer" href="${esc(data.tracking_url)}">Track delivery ↗</a></p>`:''}<p class="muted">${esc(data.customer.name)} · ${esc(data.customer.city)}, ${esc(data.customer.country)}</p><div class="actions"><button id="refresh-order" class="secondary">Refresh status</button>${data.status==='PAYMENT_CAPTURED'?'<button id="download-receipt">Download invoice</button>':''}${data.status==='PENDING_PAYMENT'?'<button id="retry-payment">Complete payment</button>':''}</div>`;
  $('#order-content').onclick=event=>{
    const prodId=event.target.closest('[data-product-id]')?.dataset.productId;
    if(prodId){
      $('#order-dialog').close();
      const card=document.querySelector(`[data-add="${prodId}"]`)?.closest('.product');
      if(card){
        card.scrollIntoView({behavior:'smooth',block:'center'});
        card.style.outline='3px solid var(--green)';
        setTimeout(()=>{card.style.outline='';},2000);
      }
    }
  };
  $('#refresh-order').onclick=()=>showOrder(id,token).catch(report);
  if($('#download-receipt')) $('#download-receipt').onclick=async()=>{
    try{const response=await fetch(`/api/orders/${id}/invoice`,{headers:{'X-Order-Token':token}});if(!response.ok)throw new Error('Invoice is not available yet.');const blob=new Blob([await response.text()],{type:'text/html'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=`invoice-${data.invoice_number}.html`;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}catch(error){report(error);}
  };
  if($('#retry-payment')) $('#retry-payment').onclick=async()=>{
    try{const saved=savedOrders.find(o=>o.order_id===id);if(!saved?.gateway)throw new Error('Please contact the store to resume payment.');if(Date.parse(saved.gateway.expires_at)<=Date.now())throw new Error('This payment reservation expired. Please start a new checkout.');await loadPayments();$('#order-dialog').close();openPayment(saved.gateway,token,data.customer);}catch(error){report(error);}
  };
  if(!$('#order-dialog').open) $('#order-dialog').showModal();
}
async function initialize(){
  try{
    [config,products]=await Promise.all([api('/api/store'),api('/api/products')]);
    document.querySelectorAll('[data-store-name]').forEach(node=>node.textContent=config.name);document.title=`${config.name} — Everyday finds`;
    if(config.support_email){$('#support-link').href=`mailto:${config.support_email}`;$('#support-link').hidden=false;}
    $('#category').innerHTML='<option value="">All categories</option>'+[...new Set(products.map(p=>p.category))].sort().map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    const codes=[...config.shipping_countries];
    const names=new Intl.DisplayNames(['en'],{type:'region'});codes.sort((a,b)=>names.of(a).localeCompare(names.of(b)));
    $('#country').innerHTML=codes.map(c=>`<option value="${esc(c)}" ${c==='IN'?'selected':''}>${esc(names.of(c))}</option>`).join('');
    for(const id of Object.keys(cart)){const p=products.find(p=>p.product_id===id);if(!p || !Number.isInteger(cart[id]) || cart[id]<1 || !p.available)delete cart[id];else cart[id]=Math.min(cart[id],p.available);}
    renderProducts();renderCart();
    if(!products.length)$('#products').innerHTML='<div class="empty">Our collection is coming soon. Check back for new arrivals.</div>';
  }catch(error){report(error);$('#products').innerHTML='<div class="empty">We could not load the collection. Please refresh to try again.</div>';$('#product-count').textContent='';}
}
initialize();

