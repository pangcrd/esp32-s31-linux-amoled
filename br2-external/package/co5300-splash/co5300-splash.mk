################################################################################
#
# co5300-splash
#
################################################################################

CO5300_SPLASH_VERSION = local
CO5300_SPLASH_SITE = $(BR2_EXTERNAL_ESP32S31_PATH)/../tools/co5300-splash
CO5300_SPLASH_SITE_METHOD = local
CO5300_SPLASH_LICENSE = MIT, GPL-2.0 (Tux image)
CO5300_SPLASH_LICENSE_FILES = co5300-splash.c COPYING.tux

define CO5300_SPLASH_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -B -C $(@D) $(TARGET_CONFIGURE_OPTS) co5300-splash
endef

define CO5300_SPLASH_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/co5300-splash $(TARGET_DIR)/usr/bin/co5300-splash
	$(INSTALL) -D -m 0644 $(@D)/sample.rgb565 $(TARGET_DIR)/usr/share/co5300-splash/logo.rgb565
	$(INSTALL) -D -m 0644 $(@D)/tux.rgb565 $(TARGET_DIR)/usr/share/co5300-splash/tux.rgb565
	$(INSTALL) -D -m 0644 $(@D)/co5300-splash.conf $(TARGET_DIR)/etc/default/co5300-splash
endef

define CO5300_SPLASH_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(@D)/S02co5300-splash $(TARGET_DIR)/etc/init.d/S02co5300-splash
endef

$(eval $(generic-package))
