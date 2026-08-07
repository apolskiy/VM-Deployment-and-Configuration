# Jump station: Apache as a mod_proxy_balancer load balancer over the backends.
FROM httpd:2.4

COPY docker/jump/jump.conf /usr/local/apache2/conf/extra/jump.conf
RUN echo "Include conf/extra/jump.conf" >> /usr/local/apache2/conf/httpd.conf

EXPOSE 80
