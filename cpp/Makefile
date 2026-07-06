# Makefile — multicam_perception_rt C++ app (Jetson / DeepStream 7.1).
#
#   make            build ./multicam_rt
#   make clean      remove objects + binary
#
# Dependencies (all present on a stock JetPack 6 + DeepStream 7.1 image):
#   gstreamer-1.0 dev, yaml-cpp dev, DeepStream SDK headers + libs.

APP      := multicam_rt
SRCDIR   := src
SRCS     := $(wildcard $(SRCDIR)/*.cpp)
OBJS     := $(SRCS:.cpp=.o)

DS_ROOT  ?= /opt/nvidia/deepstream/deepstream
PKGS     := gstreamer-1.0 yaml-cpp

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra \
            -I$(DS_ROOT)/sources/includes \
            $(shell pkg-config --cflags $(PKGS))
LDLIBS   += $(shell pkg-config --libs $(PKGS)) \
            -L$(DS_ROOT)/lib -lnvdsgst_meta -lnvds_meta \
            -Wl,-rpath,$(DS_ROOT)/lib

all: $(APP)

$(APP): $(OBJS)
	$(CXX) -o $@ $(OBJS) $(LDLIBS)

# -MMD emits a .d file per object so header edits rebuild exactly the objects
# that include them (instead of everything).
$(SRCDIR)/%.o: $(SRCDIR)/%.cpp
	$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

-include $(OBJS:.o=.d)

clean:
	rm -f $(OBJS) $(OBJS:.o=.d) $(APP)

.PHONY: all clean
