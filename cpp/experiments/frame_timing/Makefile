# Makefile — frame_timing_probe (Jetson / DeepStream 7.1).
#
#   make            build ./frame_timing_probe
#   make clean      remove objects + binary
#
# Same dependency set as ../../Makefile, minus yaml-cpp (this tool is
# CLI-configured so a timing run is fully described by its command line).

APP      := frame_timing_probe
SRCS     := frame_timing_probe.cpp
OBJS     := $(SRCS:.cpp=.o)

DS_ROOT  ?= /opt/nvidia/deepstream/deepstream
PKGS     := gstreamer-1.0

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

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

-include $(OBJS:.o=.d)

clean:
	rm -f $(OBJS) $(OBJS:.o=.d) $(APP)

.PHONY: all clean
